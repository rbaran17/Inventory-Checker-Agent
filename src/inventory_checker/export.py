"""Results export: CSV (one row per sample item) and an Excel workbook for the working papers."""

from __future__ import annotations

import csv
import io
import re
import time
from typing import Any

from .sample import Sample

STATUS_LABEL = {"verified": "Verified", "check": "Check", "exception": "Exception", "not_scanned": "Not scanned"}
RESULT_LABEL = {"match": "Match", "near_match": "Near match - check", "mismatch": "Mismatch", "not_read": "Not read",
                "recorded": "Recorded", "n/a": ""}
DECISION_LABEL = {"accepted": "Accepted", "follow_up": "Follow up", None: ""}

# Label text, object names, notes, listing cells and listing column names come from the audited party or the
# model. A spreadsheet treats a cell starting with one of these as a formula, so such text is never written as-is.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
# The same characters where a spreadsheet may start a new cell inside the value. The file is comma-separated
# and quoted for that, but Excel set to a semicolon list separator (most of Europe) splits each line at ";"
# instead and only honours a quote at the very start of a cell, so "Laptop;=1+1;x" becomes three cells, the
# middle one a formula. A tab or a line break inside a value can start a cell the same way. Quoting cannot
# prevent this (the quotes belong to the comma layout), so the formula character itself is neutralised.
_FORMULA_INSIDE = re.compile(r"([;\t\r\n][ \t]*)(?=[=+\-@])")


def csv_safe(value: Any) -> Any:
    """Text that a spreadsheet would run as a formula gets an apostrophe in front of it, at the start of the
    value and after any semicolon, tab or line break inside it; other values pass through."""
    if not isinstance(value, str):
        return value
    value = _FORMULA_INSIDE.sub(r"\1'", value)
    return "'" + value if value.startswith(_FORMULA_START) else value


def filename(workspace: str, details: dict[str, Any]) -> str:
    base = details.get("client") or ("demo" if workspace == "demo" else "inventory-count")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower() or "inventory-count"
    return f"{slug}-count-results-{time.strftime('%Y%m%d')}"


def _rows(sample: Sample, assessment: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    header = ["Item ID", "Description", "Location", "Expected qty", "Counted qty", "Qty difference", "Status"]
    for name in sample.attribute_names:
        header += [f"{name} (expected)", f"{name} (read)", f"{name} result"]
    header += ["Other identifiers read", "Evidence scan", "Review decision", "Review note"]
    rows = []
    for r in assessment["items"]:
        row = [r["item_id"], r["description"], r["location"], r["expected_qty"], r["counted_qty"],
               r["qty_difference"], STATUS_LABEL[r["status"]]]
        for c in r["checks"]:
            row += [c["expected"], "; ".join(c["observed"]), RESULT_LABEL[c["result"]]]
        row += ["; ".join(f"{o['name']}: {', '.join(o['observed'])}" for o in r["other_attributes"]),
                r["evidence_scan"] or "", DECISION_LABEL[r["review"].get("decision")], r["review"].get("note", "")]
        rows.append(row)
    return header, rows


def results_csv(sample: Sample, assessment: dict[str, Any]) -> str:
    header, rows = _rows(sample, assessment)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([csv_safe(h) for h in header])  # attribute column names are text from the listing too
    w.writerows([["" if v is None else csv_safe(v) for v in row] for row in rows])
    return buf.getvalue()


def results_xlsx(sample: Sample, assessment: dict[str, Any], details: dict[str, Any], scans: list[dict[str, Any]],
                 ai_label: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    def clean(v: Any) -> Any:
        """A cell value openpyxl accepts. It refuses text that holds control characters (a vertical tab inside
        a label, a group separator from a barcode), and one such cell used to fail the whole export."""
        if v is None:
            return ""
        return ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v

    head_fill, head_font = PatternFill("solid", start_color="1F2A44"), Font(bold=True, color="FFFFFF")
    fills = {"Verified": "DFF3E7", "Check": "FFF1C7", "Exception": "FDE3E3", "Mismatch": "FDE3E3",
             "Near match - check": "FFF1C7", "Match": "DFF3E7", "Not read": "FFF1C7"}

    def table(ws, header: list[str], rows: list[list[Any]], color_cols: tuple[str, ...] = ()) -> None:
        ws.append([clean(h) for h in header])
        for c in ws[ws.max_row]:
            c.fill, c.font, c.alignment = head_fill, head_font, Alignment(wrap_text=True, vertical="top")
        idx = [i for i, h in enumerate(header) if h in color_cols or h.endswith(" result")]
        for row in rows:
            ws.append([clean(v) for v in row])
            for i in idx:
                cell = ws.cell(ws.max_row, i + 1)
                if (color := fills.get(str(cell.value))):
                    cell.fill = PatternFill("solid", start_color=color)
        for i, h in enumerate(header, 1):
            width = max([len(str(h))] + [len(str(r[i - 1] or "")) for r in rows]) if rows else len(h)
            ws.column_dimensions[get_column_letter(i)].width = min(48, max(10, width + 2))
        ws.freeze_panes = "B2"

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    s = assessment["summary"]
    lines = [("Inventory count results", ""), ("Client", details.get("client", "")),
             ("Location", details.get("location", "")), ("Count date", details.get("count_date", "")),
             ("Counted by", details.get("counter", "")), ("Items to be tested listing", sample.name),
             ("Generated", time.strftime("%Y-%m-%d %H:%M")), ("Detection", ai_label), ("", ""),
             ("Items to be tested", s["total"]), ("Verified", s["verified"]), ("Check", s["check"]),
             ("Exceptions", s["exception"]), ("Not scanned", s["not_scanned"]),
             ("Reviewed (of check + exceptions)", f"{s['reviewed']} of {s['to_review']}"),
             ("Objects seen but not on the listing", len(assessment["not_in_sample"]))]
    for k, v in lines:
        ws.append([k, clean(v)])
    ws["A1"].font = Font(bold=True, size=14)
    for row in ws.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 34, 48

    header, rows = _rows(sample, assessment)
    table(wb.create_sheet("Results"), header, rows, color_cols=("Status",))

    det_rows = []
    for scan in scans:
        for o in scan["objects"]:
            det_rows.append([scan["id"], scan["taken_at"], scan["kind"], o["name"], o["count"],
                             round(o["confidence"], 2), o.get("item_id") or "", o.get("match_basis", ""),
                             "; ".join(f"{a['name']}: {a['value']}" for a in o.get("attributes", [])), o.get("notes", "")])
    table(wb.create_sheet("Detections"), ["Scan", "Time", "Type", "Object", "Count", "Confidence", "Matched item",
                                          "Match basis", "Attributes read", "Notes"], det_rows)
    table(wb.create_sheet("Not on listing"), ["Scan", "Object", "Count", "Attributes read"],
          [[x["scan_id"], x["name"], x["count"], "; ".join(f"{a['name']}: {a['value']}" for a in x["attributes"])]
           for x in assessment["not_in_sample"]])
    # The workbook has no formulas of its own: anything openpyxl took for one is text from a label or the
    # listing (e.g. "=HYPERLINK(...)"), so store it as plain text.
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
