"""HTTP API and pages.

- ``/``      laptop app: workflow (items to be tested listing → phone → scan & match → review → summary),
             Engagement and Demo tabs
- ``/scan``  phone page: live camera, video segments or close-up photos
- ``/api``   JSON used by both pages

There are two independent workspaces, ``engagement`` (the uploaded items to be tested listing) and ``demo``
(everyday items);
phone scans go to whichever is active on the laptop. State is in memory: restarting clears it.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import time
from collections import deque
from importlib import resources
from typing import Any, Literal
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import detection, export, security
from .config import Settings
from .engine import build_scan, detector_from_env, rereconcile
from .sample import TEMPLATE_CSV, Sample, assess, demo_sample, parse_sample

log = logging.getLogger("inventory_checker")

Detector = detection.Detector

ALLOWED_IMAGES = {"image/jpeg", "image/png", "image/webp"}
PHONE_TIMEOUT_SECONDS = 20
BODY_OVERHEAD_BYTES = 1024 * 1024     # multipart boundaries and headers on top of MAX_UPLOAD_BYTES
KEY_ATTEMPTS, KEY_ATTEMPT_WINDOW = 10, 300   # wrong keys allowed per client address, per 5 minutes
WORKSPACES = ("engagement", "demo")


class DetailsIn(BaseModel):
    client: str = Field("", max_length=200)
    location: str = Field("", max_length=200)
    count_date: str = Field("", max_length=40)
    counter: str = Field("", max_length=200)


class WorkspaceIn(BaseModel):
    name: Literal["engagement", "demo"]


class DemoItemIn(BaseModel):
    expected_qty: int | None = Field(None, ge=0, le=100000)
    attributes: dict[str, str] | None = None


class MappingIn(BaseModel):
    item_id: str | None


class ReviewIn(BaseModel):
    decision: Literal["accepted", "follow_up"] | None = None
    note: str = Field("", max_length=2000)


# Kept for callers of the earlier name; the shared implementation lives in engine.py.
_detector_from_env = detector_from_env


def _new_workspace(sample: Sample | None = None) -> dict[str, Any]:
    return {"sample": sample, "details": DetailsIn().model_dump(), "scans": [], "images": {}, "reviews": {}}


def create_app(settings: Settings | None = None, *, detector: Detector | None = None,
               use_env_client: bool = True, ai_label: str | None = None) -> FastAPI:
    settings = settings or Settings()
    if detector is None and use_env_client:
        detector, ai_label = _detector_from_env()
    elif detector is not None and ai_label is None:
        ai_label = "custom detector"

    app = FastAPI(title="Inventory Checker Agent", version="0.4.0")
    state: dict[str, Any] = {
        "active": "engagement",
        "ws": {"engagement": _new_workspace(), "demo": _new_workspace(demo_sample())},
        "phone_seen": None, "phone_agent": "",
        # Scan IDs are unique across both tabs and never reused, so an ID always means the same scan.
        "next_scan": 1,
    }
    security.install_log_redaction()
    laptop_cookie = security.derive(settings.access_key, "laptop-session")
    phone_key = security.derive(settings.access_key, "phone-link")
    phone_cookie = security.derive(settings.access_key, "phone-session")
    key_attempts = security.AttemptLimiter(KEY_ATTEMPTS, KEY_ATTEMPT_WINDOW)
    scan_times: deque[float] = deque()
    scans_in_flight = 0
    if use_env_client and (path := os.getenv("SAMPLE_FILE")):
        with open(path, "rb") as fh:
            state["ws"]["engagement"]["sample"] = parse_sample(fh.read(), os.path.basename(path))
    app.state.data = state
    app.state.settings = settings
    app.state.ai_ready = detector is not None
    app.state.ai_label = ai_label

    def ws(name: str | None = None) -> dict[str, Any]:
        return state["ws"][name or state["active"]]

    # ------------------------------------------------------------------ access key
    def _denied(request: Request, status: int, message: str, **kw: Any) -> Response:
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": message}, status, **kw)
        return HTMLResponse(f"<p style='font:16px system-ui;padding:24px'>{message}</p>", status, **kw)

    def _body_problem(request: Request) -> Response | None:
        """Refuse an oversized body before it is read; uploads are otherwise spooled in full before any check."""
        if request.method not in ("POST", "PUT", "PATCH"):
            return None
        if "transfer-encoding" in request.headers:
            return _denied(request, 411, "Send a Content-Length header")
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return _denied(request, 400, "Invalid Content-Length header")
        if length < 0 or length > settings.max_upload_bytes + BODY_OVERHEAD_BYTES:
            return _denied(request, 413, "Request too large")
        return None

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        path = request.url.path
        if path == "/healthz":  # container health checks carry no key
            return await call_next(request)
        if (problem := _body_problem(request)) is not None:
            return problem

        role = None
        key = request.query_params.get("key")
        caller = request.client.host if request.client else "unknown"
        if key:
            if key_attempts.blocked(caller):
                return _denied(request, 429, "Too many wrong access keys from this address. Wait a few minutes.",
                               headers={"Retry-After": str(KEY_ATTEMPT_WINDOW)})
            if security.same(key, settings.access_key):
                role = "laptop"
            elif security.same(key, phone_key):
                role = "phone"
            else:
                key_attempts.record(caller)
            if role and request.method == "GET" and not path.startswith("/api"):
                # Drop the key from the address bar and keep a derived session cookie instead.
                resp = RedirectResponse(path if role == "laptop" else "/scan")
                https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
                name, value = ((security.LAPTOP_COOKIE, laptop_cookie) if role == "laptop"
                               else (security.PHONE_COOKIE, phone_cookie))
                resp.set_cookie(name, value, httponly=True, samesite="lax", secure=https,
                                max_age=security.COOKIE_MAX_AGE)
                return resp
        if role is None:
            if security.same(request.cookies.get(security.LAPTOP_COOKIE, ""), laptop_cookie):
                role = "laptop"
            elif security.same(request.cookies.get(security.PHONE_COOKIE, ""), phone_cookie):
                role = "phone"
        if role is None:
            if path == "/scan" or request.cookies.get(security.PHONE_COOKIE):
                message = "Scan the QR code on the laptop to connect this phone."
            else:
                message = ("Open the link printed in the terminal (it includes the access key), "
                           "or scan the QR code on the laptop.")
            resp = _denied(request, 401, message)
            for name in (security.LAPTOP_COOKIE, security.PHONE_COOKIE, "ic_key"):  # drop stale sessions
                if name in request.cookies:
                    resp.delete_cookie(name)
            return resp
        if role == "phone" and not security.phone_may(request.method, path):
            return _denied(request, 403, "The phone link can only scan. Use the laptop link for this.")
        return await call_next(request)

    # ------------------------------------------------------------------ pages
    def _page(name: str) -> HTMLResponse:
        html = resources.files("inventory_checker").joinpath("static", name).read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return _page("dashboard.html")

    @app.get("/scan", response_class=HTMLResponse)
    def scan_page():
        return _page("scan.html")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "ai_ready": detector is not None, "public_url": settings.public_base_url}

    # ------------------------------------------------------------------ state
    def _assessment(w: dict[str, Any]) -> dict[str, Any] | None:
        return assess(w["sample"], w["scans"], w["reviews"]) if w["sample"] else None

    def _snapshot() -> dict[str, Any]:
        seen = state["phone_seen"]
        w = ws()
        return {
            "ai_ready": detector is not None,
            "model": ai_label if detector is not None else None,
            "phone": {"connected": seen is not None and time.time() - seen < PHONE_TIMEOUT_SECONDS,
                      "last_seen_seconds": round(time.time() - seen) if seen else None},
            "active": state["active"],
            "workspaces": {n: {"has_sample": state["ws"][n]["sample"] is not None, "scans": len(state["ws"][n]["scans"])}
                           for n in WORKSPACES},
            "details": w["details"],
            "sample": w["sample"].model_dump() if w["sample"] else None,
            "scans": w["scans"],
            "assessment": _assessment(w),
        }

    @app.get("/api/state")
    def get_state():
        return _snapshot()

    @app.post("/api/workspace")
    def set_workspace(body: WorkspaceIn):
        state["active"] = body.name
        return _snapshot()

    @app.get("/api/qr")
    def qr(request: Request):
        public = settings.resolve_public_url()
        base = public or str(request.base_url).rstrip("/")
        url = f"{base}/scan?key={quote(phone_key)}"
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        buf = io.BytesIO()
        img.save(buf)
        warning = None
        if not base.startswith("https://"):
            warning = ("This address is not HTTPS, so the phone will not allow the camera. Start the tunnel "
                       "(see README) or set PUBLIC_BASE_URL. This page re-checks every few seconds.")
        return {"url": url, "svg": buf.getvalue().decode(), "public": public is not None, "warning": warning}

    @app.post("/api/ping")
    def ping(request: Request):
        state["phone_seen"] = time.time()
        state["phone_agent"] = request.headers.get("user-agent", "")[:200]
        w = ws()
        return {"ok": True, "ai_ready": detector is not None, "workspace": state["active"],
                "sample_name": w["sample"].name if w["sample"] else None,
                "summary": (_assessment(w) or {}).get("summary")}

    # ------------------------------------------------------------------ engagement: details and sample
    @app.post("/api/engagement/details")
    def set_details(body: DetailsIn):
        ws("engagement")["details"] = body.model_dump()
        return _snapshot()

    @app.post("/api/engagement/sample")
    async def upload_sample(file: UploadFile = File(...)):
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, "File too large")
        try:
            # Parsing a large workbook is slow: keep it off the event loop so scans and pages stay responsive.
            sample = await run_in_threadpool(parse_sample, data, file.filename or "sample.csv")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        w = ws("engagement")
        w.update(sample=sample, scans=[], images={}, reviews={})
        state["active"] = "engagement"
        return _snapshot()

    @app.delete("/api/engagement/sample")
    def clear_sample():
        state["ws"]["engagement"] = {**_new_workspace(), "details": ws("engagement")["details"]}
        return _snapshot()

    @app.get("/api/sample-template.csv")
    def template():
        return PlainTextResponse(TEMPLATE_CSV, media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="items_to_be_tested_template.csv"'})

    # ------------------------------------------------------------------ demo
    @app.post("/api/demo/reset")
    def reset_demo():
        state["ws"]["demo"] = _new_workspace(demo_sample())
        return _snapshot()

    @app.post("/api/demo/items/{item_id}")
    def edit_demo_item(item_id: str, body: DemoItemIn):
        item = ws("demo")["sample"].item(item_id)
        if item is None:
            raise HTTPException(404, "Item not found")
        if body.expected_qty is not None:
            item.expected_qty = body.expected_qty
        for name, value in (body.attributes or {}).items():
            if name in item.attributes:
                item.attributes[name] = value.strip()[:200]
        rereconcile(ws("demo")["sample"], ws("demo")["scans"])
        return _snapshot()

    # ------------------------------------------------------------------ scans
    @app.post("/api/scans")
    async def create_scan(frames: list[UploadFile] = File(...)):
        if detector is None:
            raise HTTPException(503, "No AI provider is configured: set ANTHROPIC_API_KEY, or the Azure OpenAI / "
                                     "OpenAI settings (see README), and restart the server")
        nonlocal scans_in_flight
        w, workspace = ws(), state["active"]
        sample = w["sample"]
        if sample is None:
            raise HTTPException(409, "Upload the items to be tested listing on the laptop first (or switch to the Demo tab)")
        if not 1 <= len(frames) <= detection.MAX_FRAMES:
            raise HTTPException(400, f"Send 1 to {detection.MAX_FRAMES} frames")
        images: list[tuple[bytes, str]] = []
        total = 0
        for f in frames:
            media_type = (f.content_type or "").split(";")[0].lower()
            if media_type not in ALLOWED_IMAGES:
                raise HTTPException(400, f"Unsupported image type {media_type or 'unknown'}")
            data = await f.read(settings.max_upload_bytes + 1)
            total += len(data)
            if total > settings.max_upload_bytes:
                raise HTTPException(413, "Frames too large")
            images.append((data, media_type))
        # Every scan is a paid model call: cap how many run at once and per minute.
        now = time.monotonic()
        while scan_times and now - scan_times[0] > 60:
            scan_times.popleft()
        if scans_in_flight >= settings.max_concurrent_scans:
            raise HTTPException(429, "The previous scan is still being analysed", headers={"Retry-After": "3"})
        if len(scan_times) >= settings.max_scans_per_minute:
            raise HTTPException(429, "Too many scans in the last minute. Pause for a moment.",
                                headers={"Retry-After": "20"})
        scan_times.append(now)
        state["phone_seen"] = time.time()
        started = time.time()
        scans_in_flight += 1
        try:
            result = await detector(images, sample)
        except Exception as exc:  # report model/API errors to the phone and the server log
            log.exception("Detection failed")
            raise HTTPException(502, f"Detection failed: {exc}") from exc
        finally:
            scans_in_flight -= 1
        if state["ws"][workspace] is not w or w["sample"] is not sample:
            raise HTTPException(409, "The listing changed while this scan was being analysed. Scan again.")
        scan_id = f"S{state['next_scan']:03d}"
        state["next_scan"] += 1
        scan = build_scan(scan_id, result, sample, frames=len(images), seconds=time.time() - started)
        w["scans"].append(scan)
        w["images"][scan_id] = [(data, media_type, hashlib.sha256(data).hexdigest()[:32])
                                for data, media_type in images]
        return {"workspace": workspace, "scan": scan, "summary": _assessment(w)["summary"]}

    def _scan(scan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(workspace, scan) for an ID, whichever tab it belongs to: the laptop may have switched tabs since
        the phone or page that is asking last refreshed."""
        for w in state["ws"].values():
            for scan in w["scans"]:
                if scan["id"] == scan_id:
                    return w, scan
        raise HTTPException(404, "Scan not found")

    def _object_index(scan: dict[str, Any], object_id: str) -> int:
        for i, obj in enumerate(scan["objects"]):
            if obj["id"] == object_id:
                return i
        raise HTTPException(404, "Object not found")

    @app.get("/api/scans/{scan_id}/frames/{index}")
    def scan_frame(scan_id: str, index: int, request: Request):
        w, _ = _scan(scan_id)
        frames = w["images"].get(scan_id)
        if not frames or not 0 <= index < len(frames):
            raise HTTPException(404, "Frame not found")
        data, media_type, digest = frames[index]
        # Revalidate on every use: a scan ID can mean a different scan after the server restarts.
        headers = {"Cache-Control": "private, no-cache", "ETag": f'"{digest}"'}
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type=media_type, headers=headers)

    @app.post("/api/scans/{scan_id}/objects/{object_id}")
    def remap(scan_id: str, object_id: str, body: MappingIn):
        w, scan = _scan(scan_id)
        index = _object_index(scan, object_id)
        if body.item_id is not None and not w["sample"].item(body.item_id):
            raise HTTPException(400, f"Unknown item {body.item_id}")
        scan["objects"][index].update(item_id=body.item_id, match_basis="auditor", mapped_by="you")
        return _snapshot()

    @app.delete("/api/scans/{scan_id}")
    def delete_scan(scan_id: str):
        for w in state["ws"].values():
            w["scans"] = [s for s in w["scans"] if s["id"] != scan_id]
            w["images"].pop(scan_id, None)
        return _snapshot()

    @app.delete("/api/scans/{scan_id}/objects/{object_id}")
    def delete_object(scan_id: str, object_id: str):
        """Remove one detected object (e.g. something in the background) and keep the rest of the scan."""
        _, scan = _scan(scan_id)
        scan["objects"].pop(_object_index(scan, object_id))
        return _phone_view()

    def _phone_view() -> dict[str, Any]:
        w = ws()
        return {"workspace": state["active"], "sample_name": w["sample"].name if w["sample"] else None,
                "scans": w["scans"], "summary": (_assessment(w) or {}).get("summary")}

    @app.get("/api/phone/scans")
    def phone_scans():
        """What the phone needs for its results sheet (lighter than /api/state)."""
        return _phone_view()

    @app.delete("/api/scans")
    def clear_scans():
        w = ws()
        w.update(scans=[], images={}, reviews={})
        return _snapshot()

    # ------------------------------------------------------------------ review
    @app.post("/api/items/{item_id}/review")
    def review(item_id: str, body: ReviewIn):
        w = ws()
        if not w["sample"] or not w["sample"].item(item_id):
            raise HTTPException(404, "Item not found")
        if body.decision is None and not body.note.strip():
            w["reviews"].pop(item_id, None)
        else:
            w["reviews"][item_id] = {"decision": body.decision, "note": body.note.strip(),
                                     "at": time.strftime("%Y-%m-%d %H:%M")}
        return _snapshot()

    # ------------------------------------------------------------------ export
    def _export_ctx() -> tuple[dict[str, Any], dict[str, Any]]:
        w = ws()
        if not w["sample"]:
            raise HTTPException(409, "No items to be tested listing loaded")
        return w, _assessment(w)

    @app.get("/api/export.csv")
    def export_csv():
        w, a = _export_ctx()
        return PlainTextResponse(export.results_csv(w["sample"], a), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{export.filename(state["active"], w["details"])}.csv"'})

    @app.get("/api/export.xlsx")
    def export_xlsx():
        w, a = _export_ctx()
        data = export.results_xlsx(w["sample"], a, w["details"], w["scans"], ai_label or "")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{export.filename(state["active"], w["details"])}.xlsx"'})

    return app


def app_from_env() -> FastAPI:
    return create_app()
