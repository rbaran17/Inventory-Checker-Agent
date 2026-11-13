"""The inventory check without a web page: the engine behind the CLI, the MCP server and the Python API.

    from inventory_checker import InventoryCheck

    check = InventoryCheck.from_file("items_to_be_tested.xlsx")      # or InventoryCheck.demo()
    check.check_paths(["photos/", "shelf.mp4"])                        # runs detection + matching
    print(check.summary)                                               # {'verified': 3, 'exception': 1, ...}
    check.export("results.xlsx")                                       # .xlsx, .csv or .json

The web app uses the same scan-building and matching code (:func:`build_scan`, :func:`rereconcile`), so a
headless run and a phone run give the same results.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from . import detection, export, providers
from .media import Frame, Segment, segments
from .sample import Sample, assess, demo_sample, parse_sample, reconcile

Detector = detection.Detector
Decision = Literal["accepted", "follow_up"]


def detector_from_env() -> tuple[Detector | None, str | None]:
    """Custom ``DETECTOR`` if set, else a MAF agent on the configured provider. Returns (detector, label)."""
    if spec := os.getenv("DETECTOR"):
        return detection.load_custom_detector(spec), f"custom: {spec}"
    built = providers.build_chat_client()
    if built is None:
        return None, None
    client, name, model = built

    async def run(frames: list[Frame], sample: Sample) -> detection.SceneResult:
        return await detection.detect(client, frames, sample)

    return run, f"{name}: {model}"


def build_scan(scan_id: str, result: detection.SceneResult, sample: Sample, *, frames: int, seconds: float,
               source: str | None = None) -> dict[str, Any]:
    """A stored scan from a detector result: objects get a stable ``id`` and are reconciled with the listing."""
    objects = []
    for n, o in enumerate(result.objects, start=1):
        # "id" stays with the object for its lifetime; positions shift when another object is removed.
        obj = {"id": str(n), **o.model_dump(), "suggested_item_id": o.matched_item_id, "mapped_by": "ai"}
        obj["item_id"], obj["match_basis"] = reconcile(sample, obj)
        objects.append(obj)
    scan = {"id": scan_id, "taken_at": time.strftime("%H:%M:%S"), "seconds": round(seconds, 1), "frames": frames,
            "kind": "photo" if frames == 1 else "video", "image_quality": result.image_quality,
            "notes": result.notes, "objects": objects}
    if source:
        scan["source"] = source
    return scan


def rereconcile(sample: Sample, scans: list[dict[str, Any]]) -> None:
    """Re-match objects after the listing changed (keeps matches the auditor set by hand)."""
    for scan in scans:
        for obj in scan["objects"]:
            if obj.get("mapped_by") != "you":
                obj["item_id"], obj["match_basis"] = reconcile(sample, obj)


class InventoryCheck:
    """One count: an items to be tested listing, the scans run against it, reviews, and exports.

    Pass ``detector`` to use your own (an async ``(frames, sample) -> SceneResult``); otherwise the provider
    configured in the environment is used (``ANTHROPIC_API_KEY``, Azure OpenAI or OpenAI; see README).
    """

    def __init__(self, sample: Sample, *, detector: Detector | None = None, ai_label: str | None = None,
                 details: dict[str, str] | None = None) -> None:
        self.sample = sample
        if detector is None:
            detector, ai_label = detector_from_env()
        self._detector = detector
        self.ai_label = ai_label or ("custom detector" if detector else None)
        self.details = {"client": "", "location": "", "count_date": "", "counter": "", **(details or {})}
        self.scans: list[dict[str, Any]] = []
        self.reviews: dict[str, dict[str, Any]] = {}
        self._next = 1

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_file(cls, path: str | Path, **kw: Any) -> InventoryCheck:
        """Load an items to be tested listing (CSV or .xlsx)."""
        path = Path(path)
        return cls(parse_sample(path.read_bytes(), path.name), **kw)

    @classmethod
    def from_bytes(cls, data: bytes, filename: str = "listing.csv", **kw: Any) -> InventoryCheck:
        return cls(parse_sample(data, filename), **kw)

    @classmethod
    def demo(cls, **kw: Any) -> InventoryCheck:
        """The twelve everyday home/office items from the app's Demo tab."""
        return cls(demo_sample(), **kw)

    @property
    def ai_ready(self) -> bool:
        return self._detector is not None

    # ------------------------------------------------------------------ scanning
    async def acheck_frames(self, frames: list[Frame], *, source: str | None = None) -> dict[str, Any]:
        """Run one scan (1 frame = close-up photo, several = video segment) and store it."""
        if self._detector is None:
            raise RuntimeError("No AI provider is configured: set ANTHROPIC_API_KEY, or the Azure OpenAI / OpenAI "
                               "settings, or pass detector=...")
        if not 1 <= len(frames) <= detection.MAX_FRAMES:
            raise ValueError(f"A scan takes 1 to {detection.MAX_FRAMES} frames")
        started = time.time()
        result = await self._detector(frames, self.sample)
        scan = build_scan(f"S{self._next:03d}", result, self.sample, frames=len(frames),
                          seconds=time.time() - started, source=source)
        self._next += 1
        self.scans.append(scan)
        return scan

    async def acheck_segments(self, segs: Iterable[Segment], *, max_scans: int | None = None,
                              on_scan: Callable[[Segment, dict[str, Any]], Any] | None = None,
                              on_error: Callable[[Segment, Exception], Any] | None = None) -> list[dict[str, Any]]:
        """Scan each segment in turn. Errors go to ``on_error`` (and are skipped) when given, else raise."""
        done = []
        for i, seg in enumerate(segs):
            if max_scans is not None and i >= max_scans:
                break
            try:
                scan = await self.acheck_frames(seg.frames, source=f"{seg.source.name} {seg.label}".strip())
            except Exception as exc:
                if on_error is None:
                    raise
                on_error(seg, exc)
                continue
            done.append(scan)
            if on_scan:
                on_scan(seg, scan)
        return done

    async def acheck_paths(self, paths: Iterable[str | Path], *, every_seconds: float = 1.0, segment_frames: int = 4,
                           **kw: Any) -> list[dict[str, Any]]:
        """Photos and videos (or folders of them): one scan per photo, one per video segment."""
        return await self.acheck_segments(segments(paths, every_seconds, segment_frames), **kw)

    def check_frames(self, frames: list[Frame], **kw: Any) -> dict[str, Any]:
        return _run(self.acheck_frames(frames, **kw))

    def check_paths(self, paths: Iterable[str | Path], **kw: Any) -> list[dict[str, Any]]:
        return _run(self.acheck_paths(paths, **kw))

    # ------------------------------------------------------------------ results and review
    def results(self) -> dict[str, Any]:
        """Per item: counted quantity, attribute results and status; plus the summary and unlisted objects."""
        return assess(self.sample, self.scans, self.reviews)

    @property
    def summary(self) -> dict[str, int]:
        return self.results()["summary"]

    def scan(self, scan_id: str) -> dict[str, Any]:
        for s in self.scans:
            if s["id"] == scan_id:
                return s
        raise KeyError(f"Scan {scan_id} not found")

    def set_match(self, scan_id: str, object_id: str, item_id: str | None) -> None:
        """Correct which listed item a detected object is (``None`` = not on the listing)."""
        if item_id is not None and not self.sample.item(item_id):
            raise KeyError(f"Unknown item {item_id}")
        obj = next((o for o in self.scan(scan_id)["objects"] if o["id"] == object_id), None)
        if obj is None:
            raise KeyError(f"Object {object_id} not found in {scan_id}")
        obj.update(item_id=item_id, match_basis="auditor", mapped_by="you")

    def remove_scan(self, scan_id: str) -> None:
        self.scan(scan_id)
        self.scans = [s for s in self.scans if s["id"] != scan_id]

    def remove_object(self, scan_id: str, object_id: str) -> None:
        scan = self.scan(scan_id)
        before = len(scan["objects"])
        scan["objects"] = [o for o in scan["objects"] if o["id"] != object_id]
        if len(scan["objects"]) == before:
            raise KeyError(f"Object {object_id} not found in {scan_id}")

    def review(self, item_id: str, decision: Decision | None, note: str = "") -> None:
        if not self.sample.item(item_id):
            raise KeyError(f"Unknown item {item_id}")
        if decision is None and not note.strip():
            self.reviews.pop(item_id, None)
        else:
            self.reviews[item_id] = {"decision": decision, "note": note.strip()[:2000],
                                     "at": time.strftime("%Y-%m-%d %H:%M")}

    # ------------------------------------------------------------------ export
    def to_dict(self) -> dict[str, Any]:
        return {"listing": self.sample.name, "details": self.details, "detection": self.ai_label,
                "results": self.results(), "scans": self.scans}

    def export(self, path: str | Path) -> Path:
        """Write results; the format follows the extension: .xlsx, .csv or .json."""
        path = Path(path)
        ext = path.suffix.lower()
        if ext == ".xlsx":
            path.write_bytes(export.results_xlsx(self.sample, self.results(), self.details, self.scans,
                                                 self.ai_label or ""))
        elif ext == ".csv":
            path.write_text(export.results_csv(self.sample, self.results()), encoding="utf-8", newline="")
        elif ext == ".json":
            path.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        else:
            raise ValueError(f"{path.name}: use .xlsx, .csv or .json")
        return path

    # ------------------------------------------------------------------ Microsoft Agent Framework tools
    def as_tools(self, *, allowed_dir: str | Path | None = None) -> list[Any]:
        """MAF function tools over this check, for use by another MAF agent or workflow.

        File access is limited to ``allowed_dir`` (default: the current directory).
        """
        from agent_framework import tool

        root = Path(allowed_dir or Path.cwd()).resolve()

        def inside(p: str) -> Path:
            full = (root / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            if full != root and root not in full.parents:
                raise ValueError(f"{p} is outside {root}")
            return full

        @tool
        async def check_media(paths: list[str], every_seconds: float = 1.0) -> str:
            """Scan photos/videos (or folders) against the items to be tested listing. Returns the new scans."""
            scans = await self.acheck_paths([inside(p) for p in paths], every_seconds=every_seconds)
            return json.dumps({"scans": scans, "summary": self.summary}, default=str)

        @tool
        def get_results() -> str:
            """Per-item results (counted qty, attribute checks, status) and the summary."""
            return json.dumps(self.results(), default=str)

        @tool
        def review_item(item_id: str, decision: Literal["accepted", "follow_up"], note: str = "") -> str:
            """Record the auditor's conclusion on an item with an exception or check status."""
            self.review(item_id, decision, note)
            return json.dumps(self.results()["summary"])

        @tool
        def export_results(path: str) -> str:
            """Write results to .xlsx, .csv or .json inside the allowed folder."""
            return str(self.export(inside(path)))

        return [check_media, get_results, review_item, export_results]


def _run(coro: Awaitable[Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    if hasattr(coro, "close"):
        coro.close()  # never awaited: close it so Python does not warn
    raise RuntimeError("An event loop is already running: use the async methods (acheck_paths, acheck_frames)")
