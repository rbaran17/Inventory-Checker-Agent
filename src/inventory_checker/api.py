"""HTTP API and pages.

- ``/``      laptop dashboard: population, sample, QR code, live detections, sample match
- ``/scan``  phone page: live camera, scan by video segments or single photos
- ``/api``   JSON used by both pages

State is in memory (this build is a proof of concept); restarting clears it.
"""

from __future__ import annotations

import io
import secrets
import time
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Any
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import detection
from .config import Settings
from .population import FAKE_POPULATION, Item, assess, draw_sample, parse_population_csv

Detector = Callable[[list[tuple[bytes, str]], list[Item]], Awaitable[detection.SceneResult]]

ALLOWED_IMAGES = {"image/jpeg", "image/png", "image/webp"}
PHONE_TIMEOUT_SECONDS = 20
DEFAULT_SAMPLE = {"size": 6, "value_threshold": 500.0, "seed": 42}


class PopulationIn(BaseModel):
    csv: str


class SampleIn(BaseModel):
    size: int = Field(6, ge=0, le=500)
    value_threshold: float | None = Field(None, ge=0)
    seed: int | None = None


class MappingIn(BaseModel):
    sku: str | None


def create_app(settings: Settings | None = None, *, detector: Detector | None = None,
               use_env_client: bool = True) -> FastAPI:
    settings = settings or Settings()
    if detector is None and use_env_client:
        client = detection.build_client()
        if client is not None:
            async def detector(frames: list[tuple[bytes, str]], population: list[Item]) -> detection.SceneResult:
                return await detection.detect(client, frames, population)

    app = FastAPI(title="Inventory Checker Agent", version="0.2.0")
    state: dict[str, Any] = {"population": list(FAKE_POPULATION), "sample": {}, "sample_params": {},
                             "scans": [], "images": {}, "next_scan": 1, "phone_seen": None, "phone_agent": ""}

    def _draw(size: int, value_threshold: float | None, seed: int | None) -> None:
        state["sample"] = draw_sample(state["population"], size=size, value_threshold=value_threshold, seed=seed)
        state["sample_params"] = {"size": size, "value_threshold": value_threshold, "seed": seed}

    _draw(**DEFAULT_SAMPLE)
    app.state.data = state
    app.state.settings = settings
    app.state.ai_ready = detector is not None

    # ------------------------------------------------------------------ access key
    @app.middleware("http")
    async def require_key(request: Request, call_next):
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

    # ------------------------------------------------------------------ state
    def _snapshot() -> dict[str, Any]:
        seen = state["phone_seen"]
        return {
            "ai_ready": detector is not None,
            "model": detection.model_name() if detector is not None else None,
            "phone": {"connected": seen is not None and time.time() - seen < PHONE_TIMEOUT_SECONDS,
                      "last_seen_seconds": round(time.time() - seen) if seen else None,
                      "device": state["phone_agent"]},
            "population": [{**i.model_dump(), "value": i.value, "sampled": i.sku in state["sample"],
                            "sample_reason": state["sample"].get(i.sku)} for i in state["population"]],
            "sample_params": state["sample_params"],
            "scans": state["scans"],
            "assessment": assess(state["population"], state["sample"], state["scans"]),
        }

    @app.get("/api/state")
    def get_state():
        return _snapshot()

    @app.get("/api/qr")
    def qr():
        url = f"{settings.public_base_url.rstrip('/')}/scan?key={quote(settings.access_key)}"
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        buf = io.BytesIO()
        img.save(buf)
        return {"url": url, "svg": buf.getvalue().decode()}

    @app.post("/api/ping")
    def ping(request: Request):
        state["phone_seen"] = time.time()
        state["phone_agent"] = request.headers.get("user-agent", "")[:200]
        return {"ok": True, "ai_ready": detector is not None, "sample_size": len(state["sample"])}

    # ------------------------------------------------------------------ population and sample
    def _rematch() -> None:
        valid = {i.sku: i.description for i in state["population"]}
        for scan in state["scans"]:
            for obj in scan["objects"]:
                if obj.get("matched_sku") not in valid:
                    obj["matched_sku"] = None
                obj["matched_description"] = valid.get(obj.get("matched_sku") or "")

    @app.post("/api/population")
    def set_population(body: PopulationIn):
        try:
            state["population"] = parse_population_csv(body.csv)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _draw(**{**DEFAULT_SAMPLE, **state["sample_params"]})
        _rematch()
        return _snapshot()

    @app.post("/api/population/reset")
    def reset_population():
        state["population"] = list(FAKE_POPULATION)
        _draw(**DEFAULT_SAMPLE)
        _rematch()
        return _snapshot()

    @app.post("/api/sample")
    def sample(body: SampleIn):
        _draw(body.size, body.value_threshold, body.seed)
        return _snapshot()

    # ------------------------------------------------------------------ scans
    @app.post("/api/scans")
    async def create_scan(frames: list[UploadFile] = File(...)):
        if detector is None:
            raise HTTPException(503, "AI is not configured: set ANTHROPIC_API_KEY and restart the server")
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
            result = await detector(images, state["population"])
        except Exception as exc:
            raise HTTPException(502, f"Detection failed: {exc}") from exc
        scan_id = f"scan_{state['next_scan']:03d}"
        state["next_scan"] += 1
        names = {i.sku: i.description for i in state["population"]}
        scan = {
            "id": scan_id, "taken_at": time.strftime("%H:%M:%S"), "seconds": round(time.time() - started, 1),
            "frames": len(images), "image_quality": result.image_quality, "notes": result.notes,
            "objects": [{**o.model_dump(), "matched_description": names.get(o.matched_sku or ""),
                         "sampled": (o.matched_sku or "") in state["sample"], "mapped_by": "ai"}
                        for o in result.objects],
        }
        state["scans"].append(scan)
        state["images"][scan_id] = images
        return {"scan": scan, "assessment": assess(state["population"], state["sample"], state["scans"])}

    @app.get("/api/scans/{scan_id}/frames/{index}")
    def scan_frame(scan_id: str, index: int):
        frames = state["images"].get(scan_id)
        if not frames or not 0 <= index < len(frames):
            raise HTTPException(404, "Frame not found")
        data, media_type = frames[index]
        return Response(data, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/scans/{scan_id}/objects/{index}")
    def remap(scan_id: str, index: int, body: MappingIn):
        scan = next((s for s in state["scans"] if s["id"] == scan_id), None)
        if scan is None or not 0 <= index < len(scan["objects"]):
            raise HTTPException(404, "Object not found")
        names = {i.sku: i.description for i in state["population"]}
        if body.sku is not None and body.sku not in names:
            raise HTTPException(400, f"Unknown SKU {body.sku}")
        obj = scan["objects"][index]
        obj.update(matched_sku=body.sku, matched_description=names.get(body.sku or ""),
                   sampled=(body.sku or "") in state["sample"], mapped_by="you")
        return _snapshot()

    @app.delete("/api/scans/{scan_id}")
    def delete_scan(scan_id: str):
        state["scans"] = [s for s in state["scans"] if s["id"] != scan_id]
        state["images"].pop(scan_id, None)
        return _snapshot()

    @app.delete("/api/scans")
    def clear_scans():
        state["scans"].clear()
        state["images"].clear()
        return _snapshot()

    return app


def app_from_env() -> FastAPI:
    return create_app()
