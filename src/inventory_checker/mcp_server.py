"""MCP server: lets any MCP client (Claude Code, Claude Desktop, IDE agents, your own agent) run an inventory check.

    inventory-checker mcp                              # stdio, for local clients
    inventory-checker mcp --transport http --port 8765 # streamable HTTP on 127.0.0.1

Tools work on one count at a time: load a listing (or the demo), scan photos/videos from disk or an image
sent inline, look at and correct results, record review decisions, export. File paths are confined to
``--allowed-dir`` (default: the folder the server was started in).
"""

from __future__ import annotations

import base64
import binascii
import functools
import inspect
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .engine import Detector, InventoryCheck
from .media import MediaError, segments

MAX_INLINE_IMAGE_BYTES = 15 * 1024 * 1024
STATUSES = ("verified", "check", "exception", "not_scanned")

INSTRUCTIONS = """Inventory count verification. Typical flow:
1. load_listing (CSV/XLSX path) or load_demo_listing. The listing has item IDs, descriptions, expected
   quantities and attributes to verify (serial number, model number, ...).
2. check_media with photo/video paths (or check_image with a base64 image). Each photo or 4-frame video
   segment is one model call: objects are identified, counted, their label attributes read, and matched to
   listed items.
3. get_results (optionally filtered by status) to see Verified / Check / Exception / Not scanned items.
4. Correct wrong matches with set_match; record conclusions with review_item.
5. export_results to .xlsx, .csv or .json.
Expected attribute values are never sent to the model; comparisons are done in code."""


def _listing_summary(check: InventoryCheck) -> dict[str, Any]:
    s = check.sample
    return {"name": s.name, "items": len(s.items), "attribute_names": s.attribute_names,
            "listing": [{"item_id": i.item_id, "description": i.description, "expected_qty": i.expected_qty,
                         "location": i.location} for i in s.items]}


def _compact_scan(scan: dict[str, Any]) -> dict[str, Any]:
    return {"id": scan["id"], "source": scan.get("source"), "kind": scan["kind"], "image_quality": scan["image_quality"],
            "notes": scan["notes"],
            "objects": [{"id": o["id"], "name": o["name"], "count": o["count"], "confidence": round(o["confidence"], 2),
                         "item_id": o.get("item_id"), "match_basis": o.get("match_basis"),
                         "attributes": {a["name"]: a["value"] for a in o.get("attributes", [])}}
                        for o in scan["objects"]]}


def _friendly(fn):
    """Report mistakes the agent can fix (no listing, bad path, unknown id) as tool errors it can read.

    MCP SDK v2 hides the text of any other exception from the client and reports a generic crash.
    """
    expected = (ValueError, KeyError, RuntimeError, OSError)

    def message(exc: Exception) -> str:
        return str(exc).strip("'\"") if isinstance(exc, KeyError) else str(exc)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*a, **k):
            try:
                return await fn(*a, **k)
            except expected as exc:
                raise ToolError(message(exc)) from exc
    else:
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except expected as exc:
                raise ToolError(message(exc)) from exc
    return wrapper


def build_server(*, allowed_dir: str | Path | None = None, detector: Detector | None = None,
                 ai_label: str | None = None) -> MCPServer:
    root = Path(allowed_dir or Path.cwd()).resolve()
    state: dict[str, InventoryCheck | None] = {"check": None}
    server = MCPServer(name="inventory-checker", title="Inventory Checker", instructions=INSTRUCTIONS)

    def inside(p: str) -> Path:
        full = Path(p) if Path(p).is_absolute() else root / p
        full = full.resolve()
        if full != root and root not in full.parents:
            raise ValueError(f"{p} is outside the allowed folder {root}")
        return full

    def current() -> InventoryCheck:
        if state["check"] is None:
            raise ValueError("No listing loaded: call load_listing or load_demo_listing first")
        return state["check"]

    def new_check(make) -> dict[str, Any]:
        state["check"] = make(detector=detector, ai_label=ai_label)
        out = _listing_summary(state["check"])
        out["ai_ready"] = state["check"].ai_ready
        out["detection"] = state["check"].ai_label
        return out

    # ------------------------------------------------------------------ listing
    @server.tool()
    @_friendly
    def load_listing(path: str) -> dict[str, Any]:
        """Load the items to be tested listing from a CSV or Excel (.xlsx) file. Replaces any count in progress."""
        return new_check(lambda **kw: InventoryCheck.from_file(inside(path), **kw))

    @server.tool()
    @_friendly
    def load_listing_csv(csv_text: str, name: str = "listing.csv") -> dict[str, Any]:
        """Load the items to be tested listing from CSV text (header row + one row per item)."""
        return new_check(lambda **kw: InventoryCheck.from_bytes(csv_text.encode("utf-8"), name, **kw))

    @server.tool()
    @_friendly
    def load_demo_listing() -> dict[str, Any]:
        """Load the demo listing: twelve everyday home/office items, attributes recorded rather than verified."""
        return new_check(InventoryCheck.demo)

    @server.tool()
    @_friendly
    def set_details(client: str = "", location: str = "", count_date: str = "", counter: str = "") -> dict[str, str]:
        """Engagement details shown in the exported workbook."""
        check = current()
        check.details.update(client=client, location=location, count_date=count_date, counter=counter)
        return check.details

    # ------------------------------------------------------------------ scanning
    @server.tool()
    @_friendly
    async def check_media(paths: list[str], every_seconds: float = 1.0, segment_frames: int = 4,
                          max_scans: int = 50) -> dict[str, Any]:
        """Scan photos and/or videos (files or folders inside the allowed folder). One model call per photo and
        per video segment of `segment_frames` frames taken every `every_seconds`. Stops after `max_scans`."""
        check = current()
        errors: list[str] = []
        try:
            segs = list(segments([inside(p) for p in paths], every_seconds, segment_frames))
        except MediaError as exc:
            raise ValueError(str(exc)) from exc
        scans = await check.acheck_segments(segs, max_scans=max_scans,
                                            on_error=lambda seg, exc: errors.append(f"{seg.source.name} {seg.label}: {exc}"))
        return {"scans": [_compact_scan(s) for s in scans], "skipped": max(0, len(segs) - max_scans),
                "errors": errors, "summary": check.summary}

    @server.tool()
    @_friendly
    async def check_image(data_base64: str, media_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg",
                          label: str = "inline image") -> dict[str, Any]:
        """Scan one image sent inline as base64 (no file needed), e.g. a photo pasted into the chat."""
        check = current()
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data_base64 is not valid base64") from exc
        if not data or len(data) > MAX_INLINE_IMAGE_BYTES:
            raise ValueError("Image is empty or larger than 15 MB")
        scan = await check.acheck_frames([(data, media_type)], source=label)
        return {"scan": _compact_scan(scan), "summary": check.summary}

    # ------------------------------------------------------------------ results and review
    @server.tool()
    @_friendly
    def get_results(status: Literal["verified", "check", "exception", "not_scanned", "all"] = "all") -> dict[str, Any]:
        """Per-item results: expected vs counted quantity, each attribute's expected/read values and result
        (match, near_match, mismatch, not_read, recorded), overall status, and review decision."""
        res = current().results()
        items = res["items"] if status == "all" else [r for r in res["items"] if r["status"] == status]
        return {"summary": res["summary"], "items": items, "not_on_listing": res["not_in_sample"]}

    @server.tool()
    @_friendly
    def list_scans() -> list[dict[str, Any]]:
        """Every scan so far with its detected objects (ids are needed for set_match / remove_object)."""
        return [_compact_scan(s) for s in current().scans]

    @server.tool()
    @_friendly
    def set_match(scan_id: str, object_id: str, item_id: str | None = None) -> dict[str, int]:
        """Correct which listed item a detected object is; item_id null means 'not on the listing'."""
        check = current()
        check.set_match(scan_id, object_id, item_id)
        return check.summary

    @server.tool()
    @_friendly
    def remove_scan(scan_id: str) -> dict[str, int]:
        """Discard a scan (e.g. a blurry or accidental one)."""
        check = current()
        check.remove_scan(scan_id)
        return check.summary

    @server.tool()
    @_friendly
    def remove_object(scan_id: str, object_id: str) -> dict[str, int]:
        """Discard one detected object from a scan (e.g. something in the background)."""
        check = current()
        check.remove_object(scan_id, object_id)
        return check.summary

    @server.tool()
    @_friendly
    def review_item(item_id: str, decision: Literal["accepted", "follow_up"] | None, note: str = "") -> dict[str, int]:
        """Record the auditor's conclusion on an item (decision null and empty note clears it)."""
        check = current()
        check.review(item_id, decision, note)
        return check.summary

    @server.tool()
    @_friendly
    def export_results(path: str) -> dict[str, str]:
        """Write results to a .xlsx, .csv or .json file inside the allowed folder."""
        written = current().export(inside(path))
        return {"path": str(written)}

    return server

