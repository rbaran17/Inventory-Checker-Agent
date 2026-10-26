"""Example plug-in detector.

Run the app with ``DETECTOR=examples.custom_detector:detect`` (from the repository root) to replace the
hosted model with your own code, for example a local YOLO / ONNX model, an internal vision service, or a
barcode reader. The function receives the frames of one scan and the client population, and returns a
``SceneResult``. The app takes care of matching ``matched_sku`` against the sample and of the UI.
"""

from __future__ import annotations

from inventory_checker.detection import DetectedObject, SceneResult
from inventory_checker.population import Item


async def detect(frames: list[tuple[bytes, str]], population: list[Item]) -> SceneResult:
    """Toy detector: reports the first population item once per scan. Replace with a real model."""
    if not population:
        return SceneResult(notes="empty population")
    first = population[0]
    return SceneResult(
        objects=[DetectedObject(name=first.description.lower(), count=1, confidence=0.5, matched_sku=first.sku,
                                notes=f"example detector, {len(frames)} frame(s) received")],
        image_quality="usable",
        notes="examples.custom_detector",
    )
