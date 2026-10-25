"""Observation service: business rules from the PRD.

Covers planning (sessions, zones, listing, sampling), phone pairing, evidence ingestion
(hash re-check, two clocks, write-once), versioned records, open-items check, close/lock,
and review. Every state change is written to the append-only audit trail.
"""

from __future__ import annotations

import csv
import io
import random
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from .models import (
    RECORD_DATA_MODELS,
    Device,
    Evidence,
    FieldSource,
    ListingItem,
    OpenItem,
    Record,
    RecordType,
    ReviewAction,
    ReviewEvent,
    Session,
    SessionStatus,
    Suggestion,
    Zone,
    utcnow,
)
from .store import Database, EvidenceStore, sha256_hex

PAIRING_TTL = timedelta(minutes=5)
CLIP_MIN_SECONDS = 5
CLIP_MAX_SECONDS = 30
LARGE_CLOCK_GAP_SECONDS = 120
ALLOWED_MEDIA = {
    "photo": {"image/jpeg", "image/png", "image/webp", "image/heic"},
    "clip": {"video/mp4", "video/webm", "video/quicktime"},
}


class ServiceError(Exception):
    status_code = 400


class NotFound(ServiceError):
    status_code = 404


class Forbidden(ServiceError):
    status_code = 403


class Conflict(ServiceError):
    status_code = 409


class IntegrityError(ServiceError):
    status_code = 422


class ObservationService:
    def __init__(self, db: Database, evidence_store: EvidenceStore):
        self.db = db
        self.evidence_store = evidence_store

    # ================================================================== planning
    def create_session(self, user: str, **fields: Any) -> Session:
        fields.setdefault("lead", user)
        session = Session(**fields)
        self.db.save_session(session)
        self.db.append_audit(user, "session.create", session_id=session.id, entity=session.id,
                             details=session.model_dump(mode="json"))
        return session

    def get_session(self, session_id: str) -> Session:
        s = self.db.get_session(session_id)
        if not s:
            raise NotFound(f"Session {session_id} not found")
        return s

    def _require_open(self, session: Session) -> None:
        if session.status != SessionStatus.OPEN:
            raise Conflict(f"Session is {session.status.value}; it takes no new captures or records")

    def _require_editable(self, session: Session) -> None:
        if session.status == SessionStatus.REVIEWED:
            raise Conflict("Session is reviewed; only follow-up notes are allowed")

    def add_zone(self, user: str, session_id: str, code: str, description: str = "") -> Zone:
        session = self.get_session(session_id)
        self._require_editable(session)
        if any(z.code == code for z in self.db.list_zones(session_id)):
            raise Conflict(f"Zone {code} already exists")
        zone = Zone(session_id=session_id, code=code, description=description)
        self.db.save_zone(zone)
        self.db.append_audit(user, "zone.create", session_id=session_id, entity=zone.id, details={"code": code})
        return zone

    def import_listing(self, user: str, session_id: str, csv_text: str) -> int:
        """Import the client's perpetual listing. Columns: sku, description, location, uom, quantity, unit_cost."""
        session = self.get_session(session_id)
        self._require_editable(session)
        reader = csv.DictReader(io.StringIO(csv_text))
        required = {"sku", "quantity"}
        if not reader.fieldnames or not required.issubset({f.strip().lower() for f in reader.fieldnames}):
            raise ServiceError(f"Listing CSV needs at least columns {sorted(required)}")
        n = 0
        for i, raw in enumerate(reader, start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            if not row.get("sku"):
                continue
            try:
                item = ListingItem(
                    sku=row["sku"], description=row.get("description", ""), location=row.get("location", ""),
                    uom=row.get("uom") or "EA", quantity=float(row.get("quantity") or 0),
                    unit_cost=float(row.get("unit_cost") or 0),
                )
            except (ValueError, ValidationError) as exc:
                raise ServiceError(f"Listing row {i}: {exc}") from exc
            self.db.save_listing_item(session_id, item)
            n += 1
        self.db.append_audit(user, "listing.import", session_id=session_id, details={"rows": n})
        return n

    def draw_sample(self, user: str, session_id: str, seed: int | None = None) -> list[ListingItem]:
        """Sheet-to-floor sample: every item over the value threshold plus a random selection."""
        session = self.get_session(session_id)
        self._require_open(session)
        items = self.db.list_listing(session_id)
        chosen: dict[str, str] = {}
        if session.sample_value_threshold is not None:
            for it in items:
                if it.extended_value >= session.sample_value_threshold:
                    chosen[it.sku] = "value_threshold"
        rest = [it for it in items if it.sku not in chosen]
        rng = random.Random(seed if seed is not None else secrets.randbits(32))
        for it in rng.sample(rest, min(session.sample_size, len(rest))):
            chosen[it.sku] = "random"
        for it in items:
            it.sampled = it.sku in chosen
            it.sample_reason = chosen.get(it.sku)
            self.db.save_listing_item(session_id, it)
        self.db.append_audit(user, "sample.draw", session_id=session_id,
                             details={"seed": seed, "selected": chosen})
        return [it for it in items if it.sampled]

    # ================================================================== pairing
    def create_pairing_token(self, user: str, session_id: str) -> tuple[str, datetime]:
        session = self.get_session(session_id)
        self._require_open(session)
        token = secrets.token_urlsafe(24)
        expires = utcnow() + PAIRING_TTL
        self.db.save_pairing(sha256_hex(token.encode()), session_id, expires.isoformat(), user)
        self.db.append_audit(user, "pairing.create", session_id=session_id, details={"expires": expires})
        return token, expires

    def redeem_pairing(self, user: str, token: str, user_agent: str = "") -> tuple[Device, str]:
        """Bind a signed-in user's phone to a session. Returns the device and its upload bearer token."""
        consumed = self.db.consume_pairing(sha256_hex(token.encode()), utcnow().isoformat())
        if not consumed:
            raise Forbidden("Pairing code is invalid or already used")
        session_id, expires_at = consumed
        if datetime.fromisoformat(expires_at) < utcnow():
            raise Forbidden("Pairing code expired; show a new QR code")
        session = self.get_session(session_id)
        self._require_open(session)
        if any(d.user == user and d.unpaired_at is None for d in self.db.list_devices(session_id)):
            raise Conflict("You already have a camera paired to this session; unpair it first")
        device = Device(session_id=session_id, user=user, user_agent=user_agent[:300], last_seen=utcnow())
        device_token = secrets.token_urlsafe(32)
        self.db.save_device(device, token_hash=sha256_hex(device_token.encode()))
        if user not in session.observers:
            session.observers.append(user)
            self.db.save_session(session)
        self.db.append_audit(user, "device.pair", session_id=session_id, entity=device.id,
                             details={"user_agent": device.user_agent})
        return device, device_token

    def device_for_token(self, device_token: str) -> Device:
        device = self.db.get_device_by_token_hash(sha256_hex(device_token.encode()))
        if not device or device.unpaired_at is not None:
            raise Forbidden("Device is not paired")
        return device

    def unpair(self, user: str, device_id: str) -> Device:
        device = self.db.get_device(device_id)
        if not device:
            raise NotFound("Device not found")
        session = self.get_session(device.session_id)
        if user not in (device.user, session.lead):
            raise Forbidden("Only the observer or the lead can unpair this device")
        device.unpaired_at = utcnow()
        self.db.save_device(device)
        self.db.append_audit(user, "device.unpair", session_id=device.session_id, entity=device.id)
        return device

    def heartbeat(self, device: Device, battery_level: float | None, queued_uploads: int) -> Device:
        device.battery_level = battery_level
        device.queued_uploads = max(0, int(queued_uploads))
        device.last_seen = utcnow()
        self.db.save_device(device)
        return device

    # ================================================================== evidence ingestion
    def ingest_capture(
        self, device: Device, data: bytes, *, claimed_sha256: str, kind: str, media_type: str,
        capture_time: datetime, record_id: str | None = None, zone: str | None = None,
        bin_code: str | None = None, duration_seconds: float | None = None, burst_frames: int | None = None,
        gps: tuple[float, float] | None = None,
    ) -> Evidence:
        session = self.get_session(device.session_id)
        self._require_open(session)
        if kind not in ALLOWED_MEDIA:
            raise ServiceError(f"Unknown capture kind {kind}")
        base_type = media_type.split(";")[0].strip().lower()
        if base_type not in ALLOWED_MEDIA[kind]:
            raise ServiceError(f"Media type {media_type} not allowed for {kind}")
        if kind == "clip":
            if duration_seconds is None or not (CLIP_MIN_SECONDS - 0.5 <= duration_seconds <= CLIP_MAX_SECONDS + 0.5):
                raise ServiceError(f"Clips must be {CLIP_MIN_SECONDS} to {CLIP_MAX_SECONDS} seconds")
        actual = sha256_hex(data)
        if actual != claimed_sha256.lower():
            self.db.append_audit(device.user, "evidence.reject_hash_mismatch", session_id=session.id,
                                 details={"claimed": claimed_sha256, "actual": actual})
            raise IntegrityError("SHA-256 mismatch: the upload does not match the hash taken on the phone")
        existing = self.db.find_evidence_by_hash(session.id, actual)
        if existing:  # offline queue retry: idempotent
            return existing
        if record_id:
            rec = self.db.get_record(record_id)
            if not rec or rec.session_id != session.id:
                raise NotFound("Record to attach to was not found in this session")
            zone = zone or rec.data.get("zone")
        receipt = utcnow()
        if capture_time.tzinfo is None:
            capture_time = capture_time.replace(tzinfo=timezone.utc)
        evidence_id_hint = f"{session.id}-{actual}"
        uri = self.evidence_store.put(evidence_id_hint, data)
        ev = Evidence(
            session_id=session.id, kind=kind, media_type=base_type, size_bytes=len(data), sha256=actual,
            capture_time=capture_time, receipt_time=receipt,
            clock_gap_seconds=round((receipt - capture_time).total_seconds(), 3), user=device.user,
            device_id=device.id, storage_uri=uri, zone=zone, bin_code=bin_code,
            gps=gps if session.gps_enabled else None, duration_seconds=duration_seconds,
            burst_frames=burst_frames,
        )
        self.db.insert_evidence(ev)
        self.db.append_audit(device.user, "evidence.create", session_id=session.id, entity=ev.id,
                             details={"sha256": actual, "kind": kind, "clock_gap_seconds": ev.clock_gap_seconds})
        if record_id:
            self.link_evidence(device.user, ev.id, record_id)
        return self.db.get_evidence(ev.id)  # type: ignore[return-value]

    def link_evidence(self, user: str, evidence_id: str, record_id: str, *, move_from: str | None = None) -> Evidence:
        ev = self.db.get_evidence(evidence_id)
        rec = self.db.get_record(record_id)
        if not ev or not rec or ev.session_id != rec.session_id:
            raise NotFound("Evidence or record not found in the same session")
        session = self.get_session(rec.session_id)
        self._require_editable(session)
        if move_from:
            self.db.unlink_evidence(evidence_id, move_from)
        self.db.link_evidence(evidence_id, record_id)
        self.db.append_audit(user, "evidence.move" if move_from else "evidence.link", session_id=rec.session_id,
                             entity=evidence_id, details={"record_id": record_id, "from": move_from})
        return self.db.get_evidence(evidence_id)  # type: ignore[return-value]

    def save_suggestion(self, evidence_id: str, model: str, extract: dict[str, Any]) -> Suggestion:
        sug = Suggestion(evidence_id=evidence_id, model=model, extract=extract)
        self.db.save_suggestion(sug)
        ev = self.db.get_evidence(evidence_id)
        self.db.append_audit("ai-assist", "suggestion.create", session_id=ev.session_id if ev else None,
                             entity=sug.id, details={"evidence_id": evidence_id, "model": model})
        return sug

    # ================================================================== records
    def _normalise(self, session: Session, rtype: RecordType, data: dict[str, Any],
                   field_sources: dict[str, FieldSource]) -> dict[str, Any]:
        model = RECORD_DATA_MODELS[rtype]
        clean = {k: v for k, v in data.items() if k not in ("difference", "over_tolerance")}  # never typed
        try:
            parsed = model.model_validate(clean)
        except ValidationError as exc:
            raise ServiceError(str(exc)) from exc
        out = parsed.model_dump()
        if rtype == RecordType.TEST_COUNT:
            if out["client_count"] is None:
                item = self.db.get_listing_item(session.id, out["sku"])
                if item:
                    out["client_count"] = item.quantity
                    out["description"] = out["description"] or item.description
                    out["uom"] = item.uom
                    field_sources.setdefault("client_count", FieldSource.IMPORTED)
            if out["client_count"] is not None:
                diff = out["auditor_count"] - out["client_count"]
                out["difference"] = diff
                limit = max(session.tolerance_units, abs(out["client_count"]) * session.tolerance_pct / 100)
                out["over_tolerance"] = abs(diff) > limit
        if rtype == RecordType.TAG_CONTROL and out["range_end"] < out["range_start"]:
            raise ServiceError("Tag range end is before its start")
        return out

    def create_record(
        self, user: str, session_id: str, rtype: RecordType | str, data: dict[str, Any], *,
        field_sources: dict[str, FieldSource | str] | None = None, suggestion_id: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> Record:
        rtype = RecordType(rtype)
        session = self.get_session(session_id)
        self._require_open(session)
        sources = {k: FieldSource(v) for k, v in (field_sources or {}).items()}
        normalised = self._normalise(session, rtype, data, sources)
        for key in normalised:
            if key not in ("difference", "over_tolerance") and normalised[key] not in (None, "", []):
                sources.setdefault(key, FieldSource.TYPED)
        if suggestion_id and not self.db.get_suggestion(suggestion_id):
            raise NotFound("Suggestion not found")
        rec = Record(type=rtype, session_id=session_id, data=normalised, field_sources=sources,
                     suggestion_id=suggestion_id, author=user)
        self.db.insert_record_version(rec)
        self.db.append_audit(user, "record.create", session_id=session_id, entity=rec.id,
                             details={"type": rtype.value, "data": normalised,
                                      "field_sources": {k: v.value for k, v in sources.items()}})
        if any(v in (FieldSource.ACCEPTED, FieldSource.EDITED) for v in sources.values()):
            self.db.append_audit(user, "suggestion.accept", session_id=session_id, entity=rec.id,
                                 details={"suggestion_id": suggestion_id,
                                          "fields": {k: v.value for k, v in sources.items()
                                                     if v in (FieldSource.ACCEPTED, FieldSource.EDITED)}})
        for eid in evidence_ids or []:
            self.link_evidence(user, eid, rec.id)
        return rec

    def correct_record(
        self, user: str, record_id: str, changes: dict[str, Any], reason: str, *,
        field_sources: dict[str, FieldSource | str] | None = None,
    ) -> Record:
        """Corrections are new versions with a reason; the previous version stays readable."""
        if not reason or not reason.strip():
            raise ServiceError("A correction needs a reason")
        current = self.db.get_record(record_id)
        if not current:
            raise NotFound("Record not found")
        session = self.get_session(current.session_id)
        self._require_editable(session)
        sources = dict(current.field_sources)
        for k in changes:
            prior = sources.get(k)
            sources[k] = FieldSource.EDITED if prior in (FieldSource.ACCEPTED, FieldSource.EDITED) else FieldSource.TYPED
        sources.update({k: FieldSource(v) for k, v in (field_sources or {}).items()})
        merged = {**current.data, **changes}
        if current.type == RecordType.TEST_COUNT and "sku" in changes and "client_count" not in changes:
            merged["client_count"] = None  # re-pull from listing for the new SKU
        normalised = self._normalise(session, current.type, merged, sources)
        rec = current.model_copy(update={
            "version": current.version + 1, "data": normalised, "field_sources": sources, "author": user,
            "created_at": utcnow(), "reason": reason.strip(), "review_status": "pending",
        })
        self.db.insert_record_version(rec)
        self.db.append_audit(user, "record.edit", session_id=rec.session_id, entity=rec.id,
                             details={"version": rec.version, "reason": reason, "before": current.data,
                                      "after": normalised})
        return rec

    def records(self, session_id: str, rtype: RecordType | str | None = None) -> list[Record]:
        return self.db.list_current_records(session_id, RecordType(rtype).value if rtype else None)

    # ================================================================== coverage & open items
    def coverage(self, session_id: str) -> dict[str, dict[str, int]]:
        zones = {z.code: {"test_counts": 0, "observations": 0, "captures": 0} for z in self.db.list_zones(session_id)}
        unzoned = {"test_counts": 0, "observations": 0, "captures": 0}
        for r in self.records(session_id):
            bucket = zones.get(r.data.get("zone") or "", unzoned)
            bucket["test_counts" if r.type == RecordType.TEST_COUNT else "observations"] += 1
        for e in self.db.list_evidence(session_id):
            zones.get(e.zone or "", unzoned)["captures"] += 1
        if any(unzoned.values()):
            zones["(no zone)"] = unzoned
        return zones

    def open_items(self, session_id: str) -> list[OpenItem]:
        items: list[OpenItem] = []
        recs = self.records(session_id)
        counts = [r for r in recs if r.type == RecordType.TEST_COUNT]
        for r in counts:
            if not self.db.evidence_for_record(r.id):
                items.append(OpenItem(key=f"noevidence:{r.id}", kind="count_without_evidence", record_id=r.id,
                                      message=f"Test count {r.data['sku']} has no photo or clip"))
            if r.data.get("over_tolerance") and not (r.data.get("explanation") or "").strip():
                items.append(OpenItem(key=f"diff:{r.id}", kind="unexplained_difference", record_id=r.id,
                                      message=f"Test count {r.data['sku']} differs by {r.data['difference']:g} "
                                              "over tolerance with no explanation"))
        tags = [r for r in recs if r.type == RecordType.TAG_CONTROL]
        if not tags:
            items.append(OpenItem(key="notags", kind="no_tag_control", message="No tag control range recorded"))
        for r in tags:
            if r.data.get("unused") is None:
                items.append(OpenItem(key=f"unused:{r.id}", kind="tag_unused_not_recorded", record_id=r.id,
                                      message=f"Unused tags not recorded for range "
                                              f"{r.data['range_start']}-{r.data['range_end']}"))
        counted = {r.data["sku"] for r in counts if r.data.get("direction") == "sheet_to_floor"}
        for it in self.db.list_listing(session_id):
            if it.sampled and it.sku not in counted:
                items.append(OpenItem(key=f"sample:{it.sku}", kind="sample_not_counted",
                                      message=f"Sampled item {it.sku} ({it.location}) not yet counted"))
        for r in recs:
            if r.review_status == "queried":
                items.append(OpenItem(key=f"queried:{r.id}", kind="record_queried", record_id=r.id,
                                      message=f"Record {r.id} was queried by the reviewer", blocking=True))
        for d in self.db.list_devices(session_id):
            if d.unpaired_at is None and d.queued_uploads > 0:
                items.append(OpenItem(key=f"queue:{d.id}", kind="queued_uploads", blocking=True,
                                      message=f"{d.user}'s phone still has {d.queued_uploads} capture(s) queued"))
        return items

    def close_session(self, user: str, session_id: str, explanations: dict[str, str] | None = None) -> Session:
        session = self.get_session(session_id)
        self._require_open(session)
        explanations = {k: v.strip() for k, v in (explanations or {}).items() if v and v.strip()}
        remaining = [i for i in self.open_items(session_id) if i.blocking or i.key not in explanations]
        if remaining:
            raise Conflict("Open items must be resolved or explained before closing: "
                           + "; ".join(i.message for i in remaining))
        session.status = SessionStatus.CLOSED
        session.closed_at = utcnow()
        session.closed_by = user
        session.close_explanations = explanations
        self.db.save_session(session)
        for d in self.db.list_devices(session_id):
            if d.unpaired_at is None:
                d.unpaired_at = session.closed_at
                self.db.save_device(d)
        self.db.append_audit(user, "session.close", session_id=session_id, details={"explanations": explanations})
        return session

    # ================================================================== review
    def review(self, user: str, record_id: str, action: ReviewAction | str, comment: str = "") -> ReviewEvent:
        action = ReviewAction(action)
        rec = self.db.get_record(record_id)
        if not rec:
            raise NotFound("Record not found")
        session = self.get_session(rec.session_id)
        if action != ReviewAction.NOTE:
            if user != session.lead:
                raise Forbidden("Only the engagement lead can review records")
            self._require_editable(session)
            if action in (ReviewAction.QUERY, ReviewAction.REJECT) and not comment.strip():
                raise ServiceError("A query or rejection needs a comment")
            if action == ReviewAction.QUERY and session.status == SessionStatus.CLOSED:
                # Querying back to the observer reopens the session for the fix.
                session.status = SessionStatus.OPEN
                self.db.save_session(session)
                self.db.append_audit(user, "session.reopen", session_id=session.id,
                                     details={"reason": f"query on {record_id}"})
            status = {ReviewAction.APPROVE: "approved", ReviewAction.QUERY: "queried",
                      ReviewAction.REJECT: "rejected"}[action]
            # Review status is metadata on the current version; record it as a new immutable version.
            new_version = rec.model_copy(update={"version": rec.version + 1, "review_status": status,
                                                 "created_at": utcnow(), "author": user,
                                                 "reason": f"review: {action.value}"})
            self.db.insert_record_version(new_version)
        ev = ReviewEvent(record_id=record_id, session_id=rec.session_id, reviewer=user, action=action, comment=comment)
        self.db.save_review(ev)
        self.db.append_audit(user, f"review.{action.value}", session_id=rec.session_id, entity=record_id,
                             details={"comment": comment})
        return ev

    def sign_off(self, user: str, session_id: str) -> Session:
        session = self.get_session(session_id)
        if user != session.lead:
            raise Forbidden("Only the engagement lead can sign off")
        if session.status != SessionStatus.CLOSED:
            raise Conflict("Close the session before sign-off")
        pending = [r for r in self.records(session_id) if r.review_status not in ("approved", "rejected")]
        if pending:
            raise Conflict(f"{len(pending)} record(s) still need review")
        session.status = SessionStatus.REVIEWED
        session.reviewed_at = utcnow()
        session.reviewed_by = user
        self.db.save_session(session)
        self.db.append_audit(user, "session.sign_off", session_id=session_id)
        return session

    # ================================================================== summary for agents / UI
    def summary(self, session_id: str) -> dict[str, Any]:
        s = self.get_session(session_id)
        recs = self.records(session_id)
        return {
            "session": s.model_dump(mode="json"),
            "zones": [z.code for z in self.db.list_zones(session_id)],
            "record_counts": {t.value: sum(1 for r in recs if r.type == t) for t in RecordType},
            "evidence_count": len(self.db.list_evidence(session_id)),
            "open_items": [i.model_dump() for i in self.open_items(session_id)],
            "devices": [d.model_dump(mode="json") for d in self.db.list_devices(session_id) if d.unpaired_at is None],
        }
