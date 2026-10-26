"""Object detection and population matching with a Microsoft Agent Framework agent.

The phone films the location and, every few seconds, sends a short segment of video frames in one
request. The model lists the distinct objects in the segment, counts each physical item once even when
it shows up in several frames, and maps it to a SKU in the client population when it is the same kind
of item. Mappings are checked against the population in code, so an invented SKU never reaches the
assessment.

The agent works with any MAF chat client (see :mod:`inventory_checker.providers`). The answer format is
one strict JSON schema that both Anthropic's and OpenAI's structured outputs accept: every property
required, ``additionalProperties: false``, and no numeric limits (ranges are enforced here instead).

To plug in a different detector (for example a local vision model), point ``DETECTOR`` at an async
callable with the signature of :data:`Detector`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from agent_framework import Agent, Content, Message
from pydantic import BaseModel, Field, field_validator

from .population import Item

MAX_FRAMES = 6

INSTRUCTIONS = """You help an auditor count inventory. You receive frames from a short video the auditor
filmed at one location (or a single photo), plus the client's inventory population.

- List each distinct kind of object that is clearly visible, with how many physical items there are.
- The frames are consecutive and overlap: count each physical item once, even if it appears in several
  frames. Count only what is visible; if items are stacked or partly hidden, count the visible ones and
  say so in `notes`.
- Ignore people, walls, floors and fixed furniture you are not asked about, and anything too small or
  blurred to identify.
- Copy any readable label, brand or code into `visible_text` (null if none).
- Set `matched_sku` to the SKU of the population item that is the same kind of object, or null if none
  fits. Match on what the object is, not on colour or brand. Never invent a SKU.
- `confidence` is your confidence in the identification and the count, from 0 to 1."""


class DetectedObject(BaseModel):
    name: str
    count: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    visible_text: str | None = None
    matched_sku: str | None = None
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


Detector = Callable[[list[tuple[bytes, str]], list[Item]], Awaitable[SceneResult]]

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

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
                "required": ["name", "count", "confidence", "visible_text", "matched_sku", "notes"],
                "properties": {
                    "name": {"type": "string", "description": "What the object is, e.g. 'coffee mug'"},
                    "count": {"type": "integer", "description": "Number of physical items, 0 or more"},
                    "confidence": {"type": "number", "description": "0 to 1"},
                    "visible_text": {**_NULLABLE_STRING, "description": "Readable label, brand or code"},
                    "matched_sku": {**_NULLABLE_STRING, "description": "SKU from the population, or null"},
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


async def detect(client: Any, frames: list[tuple[bytes, str]], population: list[Item]) -> SceneResult:
    """Identify, count and match objects in ``frames`` (list of (bytes, media type)), in one model call."""
    if not frames:
        raise ValueError("No frames to analyse")
    frames = frames[:MAX_FRAMES]
    agent = Agent(client, INSTRUCTIONS, name="inventory_detector",
                  description="Identifies and counts objects in video frames and maps them to the population.")
    population_json = json.dumps([{"sku": i.sku, "description": i.description} for i in population])
    what = "this photo" if len(frames) == 1 else f"these {len(frames)} consecutive video frames"
    message = Message(role="user", contents=[
        *(Content.from_data(data, media_type) for data, media_type in frames),
        Content.from_text(f"Client inventory population:\n{population_json}\n\n"
                          f"Identify, count and match the objects in {what}."),
    ])
    response = await agent.run(message, options={"response_format": RESPONSE_FORMAT, "max_tokens": _max_tokens()})
    result = SceneResult.model_validate_json(response.text)
    valid = {i.sku for i in population}
    for obj in result.objects:
        if obj.matched_sku and obj.matched_sku not in valid:
            obj.notes = (obj.notes + f" (model suggested unknown SKU {obj.matched_sku})").strip()
            obj.matched_sku = None
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
