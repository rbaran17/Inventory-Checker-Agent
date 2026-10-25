"""Workpaper exports: test-count schedule (CSV and Excel), observation memo, evidence package.

The evidence package holds the original bytes plus a manifest of SHA-256 hashes so a reviewer
or regulator can re-verify that nothing changed since capture.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import RecordType
from .service import ObservationService
from .store import sha256_hex

AMBER = PatternFill("solid", start_color="FFE699", end_color="FFE699")
HEADER_FILL = PatternFill("solid", start_color="00338D", end_color="00338D")
HEADER_FONT = Font(bold=True, color="FFFFFF")

SCHEDULE_COLUMNS = [
    "record_id", "version", "zone", "direction", "sku", "description", "tag_no", "uom", "client_count",
    "auditor_count", "difference", "over_tolerance", "explanation", "auditor_count_source", "evidence_ids",
    "evidence_sha256", "observer", "recorded_at", "review_status",
]


def test_count_schedule(service: ObservationService, session_id: str) -> list[dict[str, Any]]:
    rows = []
    for r in service.records(session_id, RecordType.TEST_COUNT):
        ev = service.db.evidence_for_record(r.id)
        first = service.db.record_history(r.id)[0]
        rows.append({
            "record_id": r.id, "version": r.version, "zone": r.data.get("zone"), "direction": r.data["direction"],
            "sku": r.data["sku"], "description": r.data.get("description"), "tag_no": r.data.get("tag_no"),
            "uom": r.data.get("uom"), "client_count": r.data.get("client_count"),
            "auditor_count": r.data["auditor_count"], "difference": r.data.get("difference"),
            "over_tolerance": r.data.get("over_tolerance"), "explanation": r.data.get("explanation"),
            "auditor_count_source": r.field_sources.get("auditor_count", "typed"),
            "evidence_ids": ";".join(e.id for e in ev), "evidence_sha256": ";".join(e.sha256 for e in ev),
            "observer": first.author, "recorded_at": first.created_at.isoformat(), "review_status": r.review_status,
        })
    return rows


def schedule_csv(service: ObservationService, session_id: str) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SCHEDULE_COLUMNS)
    w.writeheader()
    for row in test_count_schedule(service, session_id):
        w.writerow({k: ("" if v is None else str(v.value if hasattr(v, "value") else v)) for k, v in row.items()})
    return buf.getvalue()


def _sheet(wb: Workbook, title: str, columns: list[str], rows: list[dict[str, Any]], amber_key: str | None = None):
    ws = wb.create_sheet(title)
    ws.append(columns)
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
    for row in rows:
        values = []
        for col in columns:
            v = row.get(col)
            v = v.value if hasattr(v, "value") else v
            values.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
        ws.append(values)
        if amber_key and row.get(amber_key):
            for c in ws[ws.max_row]:
                c.fill = AMBER
    for i, col in enumerate(columns, start=1):
        width = max([len(col)] + [len(str(ws.cell(r, i).value or "")) for r in range(2, ws.max_row + 1)])
        ws.column_dimensions[get_column_letter(i)].width = min(60, width + 2)
    ws.freeze_panes = "A2"
    return ws


def schedule_xlsx(service: ObservationService, session_id: str) -> bytes:
    s = service.get_session(session_id)
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    counts = test_count_schedule(service, session_id)
    lines = [
        ("Engagement", s.engagement), ("Client", s.client), ("Location", s.location), ("Count date", s.count_date),
        ("Lead", s.lead), ("Observers", ", ".join(s.observers)), ("Status", s.status.value),
        ("Tolerance", f"{s.tolerance_units:g} units / {s.tolerance_pct:g}%"),
        ("Test counts", len(counts)), ("Over tolerance", sum(1 for r in counts if r["over_tolerance"])),
        ("Counts with evidence", sum(1 for r in counts if r["evidence_ids"])),
        ("Evidence items", len(service.db.list_evidence(session_id))),
    ]
    for k, v in lines:
        summary.append([k, v])
        summary.cell(summary.max_row, 1).font = Font(bold=True)
    summary.column_dimensions["A"].width = 22
    summary.column_dimensions["B"].width = 60

    _sheet(wb, "Test Counts", SCHEDULE_COLUMNS, counts, amber_key="over_tolerance")

    def recs(t: RecordType) -> list[dict[str, Any]]:
        return [{"record_id": r.id, "version": r.version, **r.data, "review_status": r.review_status,
                 "evidence_ids": ";".join(e.id for e in service.db.evidence_for_record(r.id))}
                for r in service.records(session_id, t)]

    _sheet(wb, "Procedures", ["record_id", "version", "area", "compliant", "note", "evidence_ids", "review_status"],
           recs(RecordType.PROCEDURE))
    _sheet(wb, "Condition Notes", ["record_id", "version", "zone", "sku", "condition", "estimated_quantity", "note",
                                   "evidence_ids", "review_status"], recs(RecordType.CONDITION))
    _sheet(wb, "Cutoff", ["record_id", "version", "type", "document_no", "date", "last_before_count", "evidence_ids",
                          "review_status"], recs(RecordType.CUTOFF))
    _sheet(wb, "Tag Control", ["record_id", "version", "range_start", "range_end", "used", "unused", "voided",
                               "evidence_ids", "review_status"], recs(RecordType.TAG_CONTROL))
    _sheet(wb, "Evidence Manifest", ["id", "kind", "media_type", "sha256", "size_bytes", "capture_time",
                                     "receipt_time", "clock_gap_seconds", "user", "device_id", "zone", "record_ids"],
           [e.model_dump(mode="json") for e in service.db.list_evidence(session_id)])
    _sheet(wb, "Close Explanations", ["item", "explanation"],
           [{"item": k, "explanation": v} for k, v in s.close_explanations.items()])
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                c.alignment = Alignment(vertical="top", wrap_text=False)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def evidence_manifest(service: ObservationService, session_id: str) -> list[dict[str, Any]]:
    return [{"evidence_id": e.id, "file": f"evidence/{e.id}{_ext(e.media_type)}", "sha256": e.sha256,
             "size_bytes": e.size_bytes, "kind": e.kind, "capture_time": e.capture_time.isoformat(),
             "receipt_time": e.receipt_time.isoformat(), "clock_gap_seconds": e.clock_gap_seconds,
             "user": e.user, "device_id": e.device_id, "record_ids": e.record_ids}
            for e in service.db.list_evidence(session_id)]


def _ext(media_type: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
            "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}.get(media_type, ".bin")


def evidence_package(service: ObservationService, session_id: str, memo_markdown: str) -> bytes:
    manifest = evidence_manifest(service, session_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for m in manifest:
            data = service.evidence_store.get(service.db.get_evidence(m["evidence_id"]).storage_uri)  # type: ignore[union-attr]
            if sha256_hex(data) != m["sha256"]:
                raise RuntimeError(f"Stored evidence {m['evidence_id']} no longer matches its hash")
            z.writestr(m["file"], data)
        z.writestr("manifest.json", json.dumps({"session_id": session_id, "algorithm": "SHA-256",
                                                "faces_blurred": False, "items": manifest}, indent=2))
        mbuf = io.StringIO()
        w = csv.writer(mbuf)
        w.writerow(["sha256", "file"])
        for m in manifest:
            w.writerow([m["sha256"], m["file"]])
        z.writestr("SHA256SUMS.csv", mbuf.getvalue())
        records = {r.id: [v.model_dump(mode="json") for v in service.db.record_history(r.id)]
                   for r in service.records(session_id)}
        z.writestr("records.json", json.dumps(records, indent=2))
        z.writestr("reviews.json", json.dumps([r.model_dump(mode="json") for r in service.db.list_reviews(session_id)],
                                              indent=2))
        z.writestr("audit_trail.json", json.dumps(service.db.list_audit(session_id), indent=2, default=str))
        z.writestr("test_count_schedule.csv", schedule_csv(service, session_id))
        z.writestr("test_count_schedule.xlsx", schedule_xlsx(service, session_id))
        z.writestr("observation_memo.md", memo_markdown)
    return buf.getvalue()
