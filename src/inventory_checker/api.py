"""HTTP API: ingestion service, observation service and the two browser pages.

- ``/``           observation app (laptop or tablet): records, evidence tray, review, export
- ``/pair``       phone capture page, opened by scanning the session's QR code
- ``/api/...``    JSON API used by both pages
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from importlib import resources
from typing import Any
from urllib.parse import unquote

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from . import export
from .agents import EvidenceExtract, build_chat_client, build_observer_copilot, draft_memo, model_name, read_evidence
from .agents.closeout import CloseoutRequest, run_closeout
from .config import Settings
from .models import Device, FieldSource, RecordType, ReviewAction
from .service import ObservationService, ServiceError
from .store import Database, LocalWormEvidenceStore


# ----------------------------------------------------------------------------- request bodies
class SessionIn(BaseModel):
    engagement: str
    client: str
    location: str
    count_date: str
    observers: list[str] = []
    count_instructions: str = ""
    tolerance_units: float = 0
    tolerance_pct: float = 0
    sample_size: int = 10
    sample_value_threshold: float | None = None
    gps_enabled: bool = False


class ZoneIn(BaseModel):
    code: str
    description: str = ""


class ListingIn(BaseModel):
    csv: str


class SampleIn(BaseModel):
    seed: int | None = None


class RecordIn(BaseModel):
    type: RecordType
    data: dict[str, Any]
    field_sources: dict[str, FieldSource] = {}
    suggestion_id: str | None = None
    evidence_ids: list[str] = []


class CorrectionIn(BaseModel):
    changes: dict[str, Any]
    reason: str


class LinkIn(BaseModel):
    record_id: str
    move_from: str | None = None


class ActiveRecordIn(BaseModel):
    record_id: str | None


class ReviewIn(BaseModel):
    action: ReviewAction
    comment: str = ""


class CloseIn(BaseModel):
    explanations: dict[str, str] = {}


class RedeemIn(BaseModel):
    token: str


class HeartbeatIn(BaseModel):
    battery_level: float | None = None
    queued_uploads: int = 0


class Approval(BaseModel):
    request_id: str
    approved: bool


class CopilotIn(BaseModel):
    message: str | None = None
    approvals: list[Approval] = []


# ----------------------------------------------------------------------------- app factory
def create_app(settings: Settings | None = None, *, service: ObservationService | None = None,
               chat_client: Any | None = None, use_env_client: bool = True) -> FastAPI:
    settings = settings or Settings()
    if service is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        service = ObservationService(Database(settings.db_path),
                                     LocalWormEvidenceStore(settings.evidence_dir, settings.retention_years))
    if chat_client is None and use_env_client:
        chat_client = build_chat_client()

    app = FastAPI(title="Inventory Checker Agent", version="0.1.0")
    app.state.service = service
    app.state.chat_client = chat_client
    app.state.active_record: dict[tuple[str, str], str] = {}  # (session, user) -> record open on the laptop
    app.state.copilots: dict[tuple[str, str], dict[str, Any]] = {}

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    # ------------------------------------------------------------------ identity
    def current_user(request: Request) -> str:
        user = request.headers.get(settings.trusted_user_header)
        if not user and settings.auth_mode == "dev":
            user = unquote(request.cookies.get("ic_user") or "") or request.query_params.get("as_user")
        if not user:
            raise HTTPException(401, "Sign in required")
        return user.strip().lower()

    def current_device(authorization: str = Header("")) -> Device:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Device token required")
        return service.device_for_token(authorization[7:].strip())

    def chat_client_or_503() -> Any:
        if app.state.chat_client is None:
            raise HTTPException(503, "AI assist is not configured (set AZURE_OPENAI_ENDPOINT and "
                                     "AZURE_OPENAI_DEPLOYMENT)")
        return app.state.chat_client

    # ------------------------------------------------------------------ pages
    def _page(name: str) -> HTMLResponse:
        html = resources.files("inventory_checker").joinpath("static", name).read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/", response_class=HTMLResponse)
    def observation_app():
        return _page("app.html")

    @app.get("/pair", response_class=HTMLResponse)
    def capture_page():
        return _page("capture.html")

    @app.get("/api/me")
    def me(user: str = Depends(current_user)):
        return {"user": user, "ai_assist": app.state.chat_client is not None, "model": model_name()}

    # ------------------------------------------------------------------ planning
    @app.post("/api/sessions")
    def create_session(body: SessionIn, user: str = Depends(current_user)):
        fields = body.model_dump()
        fields["observers"] = sorted({o.strip().lower() for o in body.observers if o.strip()})
        return service.create_session(user, **fields)

    @app.get("/api/sessions")
    def list_sessions(user: str = Depends(current_user)):
        return [s for s in service.db.list_sessions() if user == s.lead or user in s.observers]

    def _session_for(session_id: str, user: str):
        s = service.get_session(session_id)
        if user != s.lead and user not in s.observers:
            raise HTTPException(403, "You are not on this observation session")
        return s

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return service.summary(session_id)

    @app.post("/api/sessions/{session_id}/zones")
    def add_zone(session_id: str, body: ZoneIn, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return service.add_zone(user, session_id, body.code, body.description)

    @app.post("/api/sessions/{session_id}/listing")
    def import_listing(session_id: str, body: ListingIn, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return {"imported": service.import_listing(user, session_id, body.csv)}

    @app.get("/api/sessions/{session_id}/listing")
    def get_listing(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return service.db.list_listing(session_id)

    @app.post("/api/sessions/{session_id}/sample")
    def sample(session_id: str, body: SampleIn, user: str = Depends(current_user)):
        if _session_for(session_id, user).lead != user:
            raise HTTPException(403, "Only the lead draws the sample")
        return service.draw_sample(user, session_id, body.seed)

    # ------------------------------------------------------------------ pairing
    @app.post("/api/sessions/{session_id}/pairing")
    def pairing(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        token, expires = service.create_pairing_token(user, session_id)
        url = f"{settings.public_base_url.rstrip('/')}/pair?token={token}"
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        buf = io.BytesIO()
        img.save(buf)
        return {"url": url, "expires_at": expires, "qr_svg": buf.getvalue().decode()}

    @app.post("/api/pair/redeem")
    def redeem(body: RedeemIn, request: Request, user: str = Depends(current_user)):
        device, device_token = service.redeem_pairing(user, body.token, request.headers.get("user-agent", ""))
        s = service.get_session(device.session_id)
        return {"device_token": device_token, "device_id": device.id, "session": {
            "id": s.id, "client": s.client, "location": s.location, "count_date": s.count_date,
            "engagement": s.engagement}, "observer": user, "zones": [z.code for z in service.db.list_zones(s.id)]}

    @app.get("/api/sessions/{session_id}/devices")
    def devices(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return [d for d in service.db.list_devices(session_id) if d.unpaired_at is None]

    @app.delete("/api/devices/{device_id}")
    def unpair(device_id: str, user: str = Depends(current_user)):
        return service.unpair(user, device_id)

    # ------------------------------------------------------------------ phone side
    @app.get("/api/device/me")
    def device_me(device: Device = Depends(current_device)):
        s = service.get_session(device.session_id)
        rid = app.state.active_record.get((s.id, device.user))
        rec = service.db.get_record(rid) if rid else None
        return {"device_id": device.id, "observer": device.user, "session": {
            "id": s.id, "client": s.client, "location": s.location, "status": s.status.value},
            "active_record": {"id": rec.id, "type": rec.type.value, "label": rec.data.get("sku") or rec.type.value}
            if rec else None, "zones": [z.code for z in service.db.list_zones(s.id)], "gps_enabled": s.gps_enabled}

    @app.post("/api/device/heartbeat")
    def heartbeat(body: HeartbeatIn, device: Device = Depends(current_device)):
        d = service.heartbeat(device, body.battery_level, body.queued_uploads)
        return {"ok": True, "battery_level": d.battery_level, "queued_uploads": d.queued_uploads}

    @app.post("/api/device/unpair")
    def device_unpair(device: Device = Depends(current_device)):
        return service.unpair(device.user, device.id)

    @app.post("/api/capture")
    async def capture(
        file: UploadFile = File(...), sha256: str = Form(...), kind: str = Form(...),
        capture_time: str = Form(...), record_id: str | None = Form(None), zone: str | None = Form(None),
        bin_code: str | None = Form(None), duration_seconds: float | None = Form(None),
        burst_frames: int | None = Form(None), gps: str | None = Form(None),
        device: Device = Depends(current_device),
    ):
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, "Capture too large")
        record_id = record_id or app.state.active_record.get((device.session_id, device.user))
        gps_t = None
        if gps:
            lat, lon = (float(x) for x in gps.split(","))
            gps_t = (lat, lon)
        ev = service.ingest_capture(
            device, data, claimed_sha256=sha256, kind=kind, media_type=file.content_type or "",
            capture_time=datetime.fromisoformat(capture_time.replace("Z", "+00:00")), record_id=record_id,
            zone=zone or None, bin_code=bin_code or None, duration_seconds=duration_seconds,
            burst_frames=burst_frames, gps=gps_t,
        )
        return {"evidence_id": ev.id, "sha256": ev.sha256, "receipt_time": ev.receipt_time,
                "record_ids": ev.record_ids}

    # ------------------------------------------------------------------ evidence
    @app.get("/api/sessions/{session_id}/evidence")
    def list_evidence(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return service.db.list_evidence(session_id)

    @app.get("/api/evidence/{evidence_id}/content")
    def evidence_content(evidence_id: str, user: str = Depends(current_user)):
        ev = service.db.get_evidence(evidence_id)
        if not ev:
            raise HTTPException(404, "Evidence not found")
        _session_for(ev.session_id, user)
        return Response(service.evidence_store.get(ev.storage_uri), media_type=ev.media_type,
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-SHA256": ev.sha256})

    @app.post("/api/evidence/{evidence_id}/link")
    def link(evidence_id: str, body: LinkIn, user: str = Depends(current_user)):
        ev = service.db.get_evidence(evidence_id)
        if not ev:
            raise HTTPException(404, "Evidence not found")
        _session_for(ev.session_id, user)
        return service.link_evidence(user, evidence_id, body.record_id, move_from=body.move_from)

    @app.post("/api/evidence/{evidence_id}/suggest")
    async def suggest(evidence_id: str, user: str = Depends(current_user)):
        client = chat_client_or_503()
        ev = service.db.get_evidence(evidence_id)
        if not ev:
            raise HTTPException(404, "Evidence not found")
        _session_for(ev.session_id, user)
        if ev.kind != "photo":
            raise HTTPException(400, "AI assist reads photos only")
        extract: EvidenceExtract = await read_evidence(
            client, service.evidence_store.get(ev.storage_uri), ev.media_type,
            context=f"zone {ev.zone}" if ev.zone else "")
        sug = service.save_suggestion(ev.id, model_name() or "unknown", extract.model_dump())
        return sug

    @app.post("/api/sessions/{session_id}/active-record")
    def set_active(session_id: str, body: ActiveRecordIn, user: str = Depends(current_user)):
        _session_for(session_id, user)
        if body.record_id:
            app.state.active_record[(session_id, user)] = body.record_id
        else:
            app.state.active_record.pop((session_id, user), None)
        return {"active_record": body.record_id}

    # ------------------------------------------------------------------ records
    @app.post("/api/sessions/{session_id}/records")
    def create_record(session_id: str, body: RecordIn, user: str = Depends(current_user)):
        _session_for(session_id, user)
        rec = service.create_record(user, session_id, body.type, body.data, field_sources=body.field_sources,
                                    suggestion_id=body.suggestion_id, evidence_ids=body.evidence_ids)
        app.state.active_record[(session_id, user)] = rec.id
        return rec

    @app.get("/api/sessions/{session_id}/records")
    def list_records(session_id: str, type: RecordType | None = None, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return [{**r.model_dump(mode="json"),
                 "evidence": [e.id for e in service.db.evidence_for_record(r.id)],
                 "reviews": [v.model_dump(mode="json") for v in service.db.list_reviews(session_id, r.id)]}
                for r in service.records(session_id, type)]

    @app.get("/api/records/{record_id}/history")
    def history(record_id: str, user: str = Depends(current_user)):
        hist = service.db.record_history(record_id)
        if not hist:
            raise HTTPException(404, "Record not found")
        _session_for(hist[0].session_id, user)
        return hist

    @app.post("/api/records/{record_id}/corrections")
    def correct(record_id: str, body: CorrectionIn, user: str = Depends(current_user)):
        rec = service.db.get_record(record_id)
        if not rec:
            raise HTTPException(404, "Record not found")
        _session_for(rec.session_id, user)
        return service.correct_record(user, record_id, body.changes, body.reason)

    @app.post("/api/records/{record_id}/review")
    def review(record_id: str, body: ReviewIn, user: str = Depends(current_user)):
        rec = service.db.get_record(record_id)
        if not rec:
            raise HTTPException(404, "Record not found")
        _session_for(rec.session_id, user)
        return service.review(user, record_id, body.action, body.comment)

    @app.get("/api/sessions/{session_id}/coverage")
    def coverage(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return service.coverage(session_id)

    @app.get("/api/sessions/{session_id}/open-items")
    def open_items(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return service.open_items(session_id)

    @app.post("/api/sessions/{session_id}/close")
    async def close(session_id: str, body: CloseIn, user: str = Depends(current_user)):
        _session_for(session_id, user)
        result = await run_closeout(service, CloseoutRequest(session_id, user, body.explanations),
                                    app.state.chat_client)
        return JSONResponse({"status": result.status, "message": result.message, "open_items": result.open_items},
                            status_code=200 if result.status == "ready" else 409)

    @app.post("/api/sessions/{session_id}/sign-off")
    def sign_off(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return service.sign_off(user, session_id)

    # ------------------------------------------------------------------ exports
    @app.get("/api/sessions/{session_id}/export/schedule.csv")
    def export_csv(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return PlainTextResponse(export.schedule_csv(service, session_id), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="test_count_schedule_{session_id}.csv"'})

    @app.get("/api/sessions/{session_id}/export/schedule.xlsx")
    def export_xlsx(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return Response(export.schedule_xlsx(service, session_id),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":
                                 f'attachment; filename="inventory_observation_{session_id}.xlsx"'})

    @app.get("/api/sessions/{session_id}/export/memo.md")
    async def export_memo(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return PlainTextResponse(await draft_memo(service, session_id, app.state.chat_client),
                                 media_type="text/markdown")

    @app.get("/api/sessions/{session_id}/export/package.zip")
    async def export_package(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        memo = await draft_memo(service, session_id, app.state.chat_client)
        return Response(export.evidence_package(service, session_id, memo), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="evidence_{session_id}.zip"'})

    @app.get("/api/sessions/{session_id}/audit")
    def audit(session_id: str, user: str = Depends(current_user)):
        _session_for(session_id, user)
        return {"chain_valid": service.db.verify_audit_chain(), "events": service.db.list_audit(session_id)}

    # ------------------------------------------------------------------ copilot (MAF agent)
    @app.post("/api/sessions/{session_id}/copilot")
    async def copilot(session_id: str, body: CopilotIn, user: str = Depends(current_user)):
        from agent_framework import Message

        client = chat_client_or_503()
        _session_for(session_id, user)
        key = (session_id, user)
        state = app.state.copilots.get(key)
        if state is None:
            agent = build_observer_copilot(client, service, session_id, user)
            state = {"agent": agent, "session": agent.create_session(), "pending": {}}
            app.state.copilots[key] = state
        contents: list[Any] = []
        for a in body.approvals:
            req = state["pending"].pop(a.request_id, None)
            if req is None:
                raise HTTPException(404, f"No pending approval {a.request_id}")
            contents.append(req.to_function_approval_response(a.approved))
        if body.message:
            contents.append(body.message)
        if not contents:
            raise HTTPException(400, "Send a message or approvals")
        resp = await state["agent"].run(Message(role="user", contents=contents), session=state["session"])
        pending = []
        for req in resp.user_input_requests:
            state["pending"][req.id] = req
            fc = req.function_call
            args = fc.arguments if isinstance(fc.arguments, dict) else json.loads(fc.arguments or "{}")
            pending.append({"request_id": req.id, "tool": fc.name, "arguments": args})
        return {"reply": resp.text, "pending_approvals": pending}

    return app


def app_from_env() -> FastAPI:
    return create_app()
