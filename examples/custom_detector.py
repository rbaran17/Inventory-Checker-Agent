"""Example plug-in detector.

Run the app with ``DETECTOR=examples.custom_detector:detect`` (from the repository root) to replace the
hosted model with your own code, for example a local YOLO / ONNX model, an internal vision service, or a
barcode / RFID reader. The function receives the frames of one scan and the items to be tested listing,
and returns a ``SceneResult``. The app reconciles objects with the listing (attribute values first, then
``matched_item_id``), compares attributes, and drives the UI.
"""

from __future__ import annotations

from inventory_checker.detection import Attribute, DetectedObject, SceneResult
from inventory_checker.sample import Sample


async def detect(frames: list[tuple[bytes, str]], sample: Sample) -> SceneResult:
    """Toy detector: reports the first listed item once per scan. Replace with a real model."""
    if not sample.items:
        return SceneResult(notes="empty listing")
    first = sample.items[0]
    read = [Attribute(name=n, value=v) for n, v in first.attributes.items() if v][:1]
    return SceneResult(
        objects=[DetectedObject(name=first.description.lower(), count=1, confidence=0.5,
                                matched_item_id=first.item_id, attributes=read,
                                notes=f"example detector, {len(frames)} frame(s) received")],
        image_quality="usable",
        notes="examples.custom_detector",
    )
