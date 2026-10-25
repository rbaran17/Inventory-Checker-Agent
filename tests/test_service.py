from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from inventory_checker.models import FieldSource, SessionStatus, utcnow
from inventory_checker.service import Conflict, Forbidden, IntegrityError, ServiceError
from inventory_checker.store import WormViolation, sha256_hex

from .conftest import LEAD, OBS

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _photo(service, device, data=JPEG, **kw):
    return service.ingest_capture(device, data, claimed_sha256=sha256_hex(data), kind="photo",
                                  media_type="image/jpeg", capture_time=utcnow(), **kw)


def _count(service, session, sku="FG-2002", auditor=260, **kw):
    return service.create_record(OBS, session.id, "test_count",
                                 {"sku": sku, "auditor_count": auditor, "direction": "floor_to_sheet", **kw})


# ------------------------------------------------------------------ pairing
def test_pairing_token_is_single_use(service, session):
    token, _ = service.create_pairing_token(LEAD, session.id)
    service.redeem_pairing(OBS, token)
    with pytest.raises(Forbidden):
        service.redeem_pairing("other@firm.example", token)


def test_pairing_token_expires(service, session, monkeypatch):
    import inventory_checker.service as svc_mod

    token, _ = service.create_pairing_token(LEAD, session.id)
    later = utcnow() + timedelta(minutes=6)
    monkeypatch.setattr(svc_mod, "utcnow", lambda: later)
    with pytest.raises(Forbidden, match="expired"):
        service.redeem_pairing(OBS, token)


def test_one_camera_per_observer(service, session, device):
    token, _ = service.create_pairing_token(LEAD, session.id)
    with pytest.raises(Conflict):
        service.redeem_pairing(OBS, token)
    service.unpair(OBS, device.id)
    token, _ = service.create_pairing_token(LEAD, session.id)
    service.redeem_pairing(OBS, token)


def test_pairing_adds_observer(service, session, device):
    assert OBS in service.get_session(session.id).observers


# ------------------------------------------------------------------ evidence integrity
def test_hash_mismatch_rejected_and_logged(service, device):
    with pytest.raises(IntegrityError):
        service.ingest_capture(device, JPEG, claimed_sha256="0" * 64, kind="photo", media_type="image/jpeg",
                               capture_time=utcnow())
    assert any(e["action"] == "evidence.reject_hash_mismatch" for e in service.db.list_audit())


def test_capture_stores_two_clocks_and_is_idempotent(service, device):
    ev = service.ingest_capture(device, JPEG, claimed_sha256=sha256_hex(JPEG), kind="photo",
                                media_type="image/jpeg", capture_time=utcnow() - timedelta(minutes=30))
    assert ev.clock_gap_seconds >= 1700
    again = _photo(service, device)
    assert again.id == ev.id
    assert len(service.db.list_evidence(device.session_id)) == 1


def test_evidence_is_write_once(service, device):
    ev = _photo(service, device)
    with pytest.raises(WormViolation):
        service.evidence_store.put(f"{device.session_id}-{ev.sha256}", b"tampered")
    with pytest.raises(WormViolation):
        service.evidence_store.delete(ev.storage_uri)
    with pytest.raises(sqlite3.IntegrityError):
        service.db._q("UPDATE evidence SET sha256 = 'x' WHERE id = ?", (ev.id,))


def test_clip_duration_enforced(service, device):
    data = b"webm-bytes"
    with pytest.raises(ServiceError, match="5 to 30"):
        service.ingest_capture(device, data, claimed_sha256=sha256_hex(data), kind="clip", media_type="video/webm",
                               capture_time=utcnow(), duration_seconds=45)
    ev = service.ingest_capture(device, data, claimed_sha256=sha256_hex(data), kind="clip",
                                media_type="video/webm;codecs=vp9", capture_time=utcnow(), duration_seconds=12)
    assert ev.media_type == "video/webm"


def test_capture_links_to_record(service, session, device):
    rec = _count(service, session)
    ev = _photo(service, device, record_id=rec.id)
    assert ev.record_ids == [rec.id]
    assert ev.zone is None  # record had no zone
    rec_b = _count(service, session, sku="FG-2001", auditor=42, zone="B")
    assert _photo(service, device, data=b"other", record_id=rec_b.id).zone == "B"


# ------------------------------------------------------------------ records
def test_difference_is_computed_never_typed(service, session):
    rec = _count(service, session, auditor=250, difference=0, over_tolerance=False)
    assert rec.data["client_count"] == 260  # from listing
    assert rec.field_sources["client_count"] == FieldSource.IMPORTED
    assert rec.data["difference"] == -10
    assert rec.data["over_tolerance"] is True  # tolerance max(1, 2% of 260 = 5.2)


def test_within_tolerance(service, session):
    assert _count(service, session, auditor=256).data["over_tolerance"] is False


def test_correction_creates_version_with_reason(service, session):
    rec = _count(service, session, auditor=250)
    with pytest.raises(ServiceError):
        service.correct_record(OBS, rec.id, {"auditor_count": 259}, reason="")
    v2 = service.correct_record(OBS, rec.id, {"auditor_count": 259}, reason="Recount of back row")
    assert v2.version == 2 and v2.data["difference"] == -1
    hist = service.db.record_history(rec.id)
    assert [h.data["auditor_count"] for h in hist] == [250, 259]


def test_editing_ai_value_is_marked_edited(service, session):
    rec = _count(service, session, auditor=250)
    rec = service.correct_record(OBS, rec.id, {}, reason="noop", field_sources={"auditor_count": "accepted"})
    v3 = service.correct_record(OBS, rec.id, {"auditor_count": 255}, reason="fixed AI count")
    assert v3.field_sources["auditor_count"] == FieldSource.EDITED


def test_record_versions_are_immutable(service, session):
    rec = _count(service, session)
    with pytest.raises(sqlite3.IntegrityError):
        service.db._q("UPDATE records SET data = '{}' WHERE id = ?", (rec.id,))


# ------------------------------------------------------------------ audit trail
def test_audit_chain_verifies_and_is_append_only(service, session):
    _count(service, session)
    assert service.db.verify_audit_chain()
    with pytest.raises(sqlite3.IntegrityError):
        service.db._q("DELETE FROM audit")
    with pytest.raises(sqlite3.IntegrityError):
        service.db._q("UPDATE audit SET user = 'x'")


# ------------------------------------------------------------------ sampling, open items, close, review
def test_sample_includes_value_threshold_items(service, session):
    picked = {i.sku: i.sample_reason for i in service.draw_sample(LEAD, session.id, seed=1)}
    # FG-2003 = 18 * 4200 = 75,600 and FG-2006 = 24 * 2650 = 63,600 exceed 50,000
    assert picked["FG-2003"] == "value_threshold" and picked["FG-2006"] == "value_threshold"
    assert sum(1 for r in picked.values() if r == "random") == 2


def test_open_items_and_close(service, session, device):
    service.draw_sample(LEAD, session.id, seed=1)
    rec = _count(service, session, auditor=250)
    kinds = {i.kind for i in service.open_items(session.id)}
    assert {"count_without_evidence", "unexplained_difference", "no_tag_control", "sample_not_counted"} <= kinds

    service.heartbeat(device, 0.8, queued_uploads=2)
    explain = {i.key: "explained" for i in service.open_items(session.id) if not i.blocking}
    with pytest.raises(Conflict, match="queued"):
        service.close_session(OBS, session.id, explain)
    service.heartbeat(device, 0.8, queued_uploads=0)
    closed = service.close_session(OBS, session.id, explain)
    assert closed.status == SessionStatus.CLOSED
    with pytest.raises(Conflict):
        _photo(service, device, record_id=rec.id)


def test_review_sign_off_locks(service, session):
    rec = _count(service, session, auditor=260)
    service.create_record(OBS, session.id, "tag_control", {"range_start": 1, "range_end": 100, "unused": ["90-100"]})
    service.close_session(OBS, session.id, {f"noevidence:{rec.id}": "photo on paper file"})
    with pytest.raises(Forbidden):
        service.review(OBS, rec.id, "approve")
    with pytest.raises(Conflict):
        service.sign_off(LEAD, session.id)
    for r in service.records(session.id):
        service.review(LEAD, r.id, "approve")
    assert service.sign_off(LEAD, session.id).status == SessionStatus.REVIEWED
    with pytest.raises(Conflict):
        service.correct_record(OBS, rec.id, {"auditor_count": 1}, reason="late")
    service.review(OBS, rec.id, "note", "Follow-up: client posted adjustment")  # notes still allowed


def test_query_reopens_closed_session(service, session):
    rec = _count(service, session, auditor=260)
    service.create_record(OBS, session.id, "tag_control", {"range_start": 1, "range_end": 5, "unused": []})
    service.close_session(OBS, session.id, {f"noevidence:{rec.id}": "x"})
    service.review(LEAD, rec.id, "query", "Please attach the tag photo")
    assert service.get_session(session.id).status == SessionStatus.OPEN
    assert any(i.kind == "record_queried" for i in service.open_items(session.id))
