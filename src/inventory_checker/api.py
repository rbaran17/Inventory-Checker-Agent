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

import io
import logging
import os
import secrets
import time
from importlib import resources
from typing import Any, Literal
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import detection, export, providers
from .config import Settings
from .sample import TEMPLATE_CSV, Sample, assess, demo_sample, parse_sample, reconcile

log = logging.getLogger("inventory_checker")

Detector = detection.Detector

ALLOWED_IMAGES = {"image/jpeg", "image/png", "image/webp"}
PHONE_TIMEOUT_SECONDS = 20
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


def _detector_from_env() -> tuple[Detector | None, str | None]:
    """Custom ``DETECTOR`` if set, else a MAF agent on the selected provider. Returns (detector, label)."""
    if spec := os.getenv("DETECTOR"):
        return detection.load_custom_detector(spec), f"custom: {spec}"
    built = providers.build_chat_client()
    if built is None:
        return None, None
    client, name, model = built

    async def run(frames: list[tuple[bytes, str]], sample: Sample) -> detection.SceneResult:
        return await detection.detect(client, frames, sample)

    return run, f"{name}: {model}"


def _new_workspace(sample: Sample | None = None) -> dict[str, Any]:
    return {"sample": sample, "details": DetailsIn().model_dump(), "scans": [], "images": {}, "reviews": {},
            "next_scan": 1}


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
    }
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
    @app.middleware("http")
    async def require_key(request: Request, call_next):
        if request.url.path == "/healthz":  # container health checks carry no key
            return await call_next(request)
        key = request.query_params.get("key")
        if key and secrets.compare_digest(key, settings.access_key):
            if request.method == "GET" and not request.url.path.startswith("/api"):
                resp = RedirectResponse(request.url.path)  # drop the key from the address bar
                https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
                resp.set_cookie("ic_key", key, httponly=True, samesite="lax", secure=https, max_age=7 * 86400)
                return resp
            return await call_next(request)
        cookie = request.cookies.get("ic_key", "")
        if cookie and secrets.compare_digest(cookie, settings.access_key):
            return await call_next(request)
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "Open the link printed in the terminal, or scan the QR code"}, 401)
        return HTMLResponse("<p style='font:16px system-ui;padding:24px'>Open the link printed in the terminal "
                            "(it includes the access key), or scan the QR code on the laptop.</p>", 401)

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
        url = f"{base}/scan?key={quote(settings.access_key)}"
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
            sample = parse_sample(data, file.filename or "sample.csv")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        w = ws("engagement")
        w.update(sample=sample, scans=[], images={}, reviews={}, next_scan=1)
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
        _rereconcile(ws("demo"))
        return _snapshot()

    def _rereconcile(w: dict[str, Any]) -> None:
        """Re-match objects after the sample changed (keeps matches the auditor set by hand)."""
        for scan in w["scans"]:
            for obj in scan["objects"]:
                if obj.get("mapped_by") != "you":
                    obj["item_id"], obj["match_basis"] = reconcile(w["sample"], obj)

    # ------------------------------------------------------------------ scans
    @app.post("/api/scans")
    async def create_scan(frames: list[UploadFile] = File(...)):
        if detector is None:
            raise HTTPException(503, "No AI provider is configured: set ANTHROPIC_API_KEY, or the Azure OpenAI / "
                                     "OpenAI settings (see README), and restart the server")
        w, workspace = ws(), state["active"]
        if w["sample"] is None:
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
        state["phone_seen"] = time.time()
        started = time.time()
        try:
            result = await detector(images, w["sample"])
        except Exception as exc:  # report model/API errors to the phone and the server log
            log.exception("Detection failed")
            raise HTTPException(502, f"Detection failed: {exc}") from exc
        scan_id = f"S{w['next_scan']:03d}"
        w["next_scan"] += 1
        objects = []
        for o in result.objects:
            obj = {**o.model_dump(), "suggested_item_id": o.matched_item_id, "mapped_by": "ai"}
            obj["item_id"], obj["match_basis"] = reconcile(w["sample"], obj)
            objects.append(obj)
        scan = {"id": scan_id, "taken_at": time.strftime("%H:%M:%S"), "seconds": round(time.time() - started, 1),
                "frames": len(images), "kind": "photo" if len(images) == 1 else "video",
                "image_quality": result.image_quality, "notes": result.notes, "objects": objects}
        w["scans"].append(scan)
        w["images"][scan_id] = images
        return {"workspace": workspace, "scan": scan, "summary": _assessment(w)["summary"]}

    def _scan(scan_id: str) -> dict[str, Any]:
        scan = next((s for s in ws()["scans"] if s["id"] == scan_id), None)
        if scan is None:
            raise HTTPException(404, "Scan not found")
        return scan

    @app.get("/api/scans/{scan_id}/frames/{index}")
    def scan_frame(scan_id: str, index: int):
        frames = ws()["images"].get(scan_id)
        if not frames or not 0 <= index < len(frames):
            raise HTTPException(404, "Frame not found")
        data, media_type = frames[index]
        return Response(data, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/scans/{scan_id}/objects/{index}")
    def remap(scan_id: str, index: int, body: MappingIn):
        scan = _scan(scan_id)
        if not 0 <= index < len(scan["objects"]):
            raise HTTPException(404, "Object not found")
        if body.item_id is not None and not ws()["sample"].item(body.item_id):
            raise HTTPException(400, f"Unknown item {body.item_id}")
        scan["objects"][index].update(item_id=body.item_id, match_basis="auditor", mapped_by="you")
        return _snapshot()

    @app.delete("/api/scans/{scan_id}")
    def delete_scan(scan_id: str):
        w = ws()
        w["scans"] = [s for s in w["scans"] if s["id"] != scan_id]
        w["images"].pop(scan_id, None)
        return _snapshot()

    @app.delete("/api/scans/{scan_id}/objects/{index}")
    def delete_object(scan_id: str, index: int):
        """Remove one detected object (e.g. something in the background) and keep the rest of the scan."""
        scan = _scan(scan_id)
        if not 0 <= index < len(scan["objects"]):
            raise HTTPException(404, "Object not found")
        scan["objects"].pop(index)
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
