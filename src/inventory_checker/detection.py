"""Object detection, label reading and sample matching with a Microsoft Agent Framework agent.

The phone sends either a short video segment (a few overlapping frames) or one close-up photo. In one model
call the agent lists the distinct objects, counts each physical item once, reads the attributes the sample
asks for (serial number, model number, ...) from labels, and suggests which sample item each object is.

The model is given the attribute *names* only, never the expected values, so it cannot echo a value it
did not read. Values are compared with the sample in :mod:`inventory_checker.sample`.

The agent works with any MAF chat client (see :mod:`inventory_checker.providers`). The answer format is one
strict JSON schema that both Anthropic's and OpenAI's structured outputs accept: every property required,
``additionalProperties: false``, no numeric limits (ranges are enforced here instead).

To plug in a different detector (for example a local vision model), point ``DETECTOR`` at an async callable
with the signature of :data:`Detector`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from agent_framework import Agent, Content, Message
from pydantic import BaseModel, Field, field_validator

from .sample import Sample

MAX_FRAMES = 6

INSTRUCTIONS = """You help an auditor count and verify inventory. You receive either frames from a short video
the auditor filmed at one location, or a single close-up photo (often of a label), plus the listing of items
to be tested.

Objects and counts
- List each distinct kind of object that is clearly visible, with how many physical items there are.
- Video frames are consecutive and overlap: count each physical item once, even if it appears in several
  frames. Count only what is visible; if items are stacked or partly hidden, say so in `notes`.
- Ignore people, walls, floors and fixed furniture, and anything too small or blurred to identify.

Attributes
- For each object, read the attributes the auditor asks for (listed below) from labels, stickers, screens or
  engravings. Use exactly the attribute names given. Copy values character for character as printed,
  including letters, digits and dashes.
- Only report what you can actually read. If a value is not visible or not legible, leave that attribute out.
  Never guess, complete or infer a serial number or model number.
- You may also report other clearly printed identifiers (asset tag, part number, brand) under a short name.

Matching
- Set `matched_item_id` to the ID of the item to be tested that is the same kind of object, or null if none fits.
  Match on what the object is, not on colour. Never invent an ID.
- `confidence` is your confidence in the identification and the count, from 0 to 1."""


class Attribute(BaseModel):
    name: str
    value: str


class DetectedObject(BaseModel):
    name: str
    count: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    matched_item_id: str | None = None
    attributes: list[Attribute] = Field(default_factory=list)
    notes: str = ""

    @field_validator("count", mode="before")
    @classmethod
    def _non_negative(cls, v: Any) -> Any:
        return max(0, int(v)) if isinstance(v, (int, float)) else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _unit_interval(cls, v: Any) -> Any:
        return min(1.0, max(0.0, float(v))) if isinstance(v, (int, float)) else v


class SceneResult(BaseModel):
    objects: list[DetectedObject] = Field(default_factory=list)
    image_quality: Literal["good", "usable", "poor"] = "usable"
    notes: str = ""


Detector = Callable[[list[tuple[bytes, str]], Sample], Awaitable[SceneResult]]

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objects", "image_quality", "notes"],
    "properties": {
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "count", "confidence", "matched_item_id", "attributes", "notes"],
                "properties": {
                    "name": {"type": "string", "description": "What the object is, e.g. 'laptop computer'"},
                    "count": {"type": "integer", "description": "Number of physical items, 0 or more"},
                    "confidence": {"type": "number", "description": "0 to 1"},
                    "matched_item_id": {"anyOf": [{"type": "string"}, {"type": "null"}],
                                        "description": "ID of the sample item, or null"},
                    "attributes": {
                        "type": "array",
                        "description": "Attributes actually read from the object; omit unreadable ones",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "value"],
                            "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                        },
                    },
                    "notes": {"type": "string"},
                },
            },
        },
        "image_quality": {"type": "string", "enum": ["good", "usable", "poor"]},
        "notes": {"type": "string"},
    },
}

# OpenAI-style envelope; MAF's Anthropic and OpenAI clients both read it.
RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "scene_result", "schema": RESPONSE_SCHEMA, "strict": True},
}


def _max_tokens() -> int:
    return int(os.getenv("AI_MAX_TOKENS") or os.getenv("CLAUDE_MAX_TOKENS") or "4000")


def build_prompt(frames: int, sample: Sample) -> str:
    items = json.dumps([{"id": i.item_id, "description": i.description} for i in sample.items])
    attrs = ", ".join(sample.attribute_names) or "(none requested; report any clearly printed identifiers)"
    what = "this close-up photo" if frames == 1 else f"these {frames} consecutive video frames"
    return (f"Items to be tested:\n{items}\n\nAttributes to read: {attrs}\n\n"
            f"Identify, count, read and match the objects in {what}.")


async def detect(client: Any, frames: list[tuple[bytes, str]], sample: Sample) -> SceneResult:
    """Identify, count, read and match objects in ``frames`` (list of (bytes, media type)), in one model call."""
    if not frames:
        raise ValueError("No frames to analyse")
    frames = frames[:MAX_FRAMES]
    agent = Agent(client, INSTRUCTIONS, name="inventory_detector",
                  description="Identifies, counts and reads labels of objects and maps them to the audit sample.")
    message = Message(role="user", contents=[
        *(Content.from_data(data, media_type) for data, media_type in frames),
        Content.from_text(build_prompt(len(frames), sample)),
    ])
    response = await agent.run(message, options={"response_format": RESPONSE_FORMAT, "max_tokens": _max_tokens()})
    result = SceneResult.model_validate_json(response.text)
    valid = {i.item_id for i in sample.items}
    for obj in result.objects:
        obj.attributes = [a for a in obj.attributes if a.name.strip() and a.value.strip()]
        if obj.matched_item_id and obj.matched_item_id not in valid:
            obj.notes = (obj.notes + f" (model suggested unknown item {obj.matched_item_id})").strip()
            obj.matched_item_id = None
    return result


def load_custom_detector(spec: str) -> Detector:
    """Load ``package.module:callable`` named by the ``DETECTOR`` setting."""
    import importlib

    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"DETECTOR must look like 'package.module:function', got {spec!r}")
    fn = getattr(importlib.import_module(module_name), attr)
    if not callable(fn):
        raise TypeError(f"{spec} is not callable")
    return fn
