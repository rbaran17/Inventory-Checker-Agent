"""Object detection and population match with a Microsoft Agent Framework agent on Claude Haiku 5.5.

The phone films the location and, every few seconds, sends a short segment of video frames in one
request. Claude lists the distinct objects in the segment, counts each physical item once even when
it shows up in several frames, and maps it to a SKU in the client population when it is the same
kind of item. Mappings are checked against the population in code, so an invented SKU never reaches
the assessment.

Cost controls: the cheapest current Claude model, effort ``low``, a small output cap, frames
downscaled on the phone, and one call per segment rather than per frame.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agent_framework import Agent, Content, Message
from pydantic import BaseModel, Field

from .population import Item

DEFAULT_MODEL = "claude-haiku-5-5"
MAX_FRAMES = 6

INSTRUCTIONS = """You help an auditor count inventory. You receive frames from a short video the auditor
filmed at one location (or a single photo), plus the client's inventory population.

- List each distinct kind of object that is clearly visible, with how many physical items there are.
- The frames are consecutive and overlap: count each physical item once, even if it appears in several
  frames. Count only what is visible; if items are stacked or partly hidden, count the visible ones and
  say so in `notes`.
- Ignore people, walls, floors and fixed furniture you are not asked about, and anything too small or
  blurred to identify.
- Copy any readable label, brand or code into `visible_text`.
- Set `matched_sku` to the SKU of the population item that is the same kind of object, or null if none
  fits. Match on what the object is, not on colour or brand. Never invent a SKU.
- `confidence` is your confidence in the identification and the count, 0 to 1."""


class DetectedObject(BaseModel):
    name: str
    count: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    visible_text: str | None = None
    matched_sku: str | None = None
    notes: str = ""


class SceneResult(BaseModel):
    objects: list[DetectedObject] = Field(default_factory=list)
    image_quality: Literal["good", "usable", "poor"] = "usable"
    notes: str = ""


def model_name() -> str:
    return os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)


def _client_class() -> type:
    from agent_framework_anthropic import AnthropicClient

    class HaikuClient(AnthropicClient):
        """Anthropic client with this app's request defaults."""

        def _prepare_options(self, messages: Sequence[Any], options: Mapping[str, Any], request_state: Any = None,
                             /, **kwargs: Any) -> dict[str, Any]:
            opts = dict(options)
            if not opts.get("max_tokens"):
                opts["max_tokens"] = int(os.getenv("CLAUDE_MAX_TOKENS", "4000"))
            output_config = dict(opts.get("output_config") or {})
            output_config.setdefault("effort", os.getenv("CLAUDE_EFFORT", "low"))
            opts["output_config"] = output_config
            return super()._prepare_options(messages, opts, request_state, **kwargs)

    return HaikuClient


def build_client(*, api_key: str | None = None, base_url: str | None = None) -> Any | None:
    """Claude client, or None when no ``ANTHROPIC_API_KEY`` is set."""
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    kwargs: dict[str, Any] = {"model": model_name(), "api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return _client_class()(**kwargs)


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
    response = await agent.run(message, options={"response_format": SceneResult})
    result = response.value if response.value is not None else SceneResult.model_validate_json(response.text)
    valid = {i.sku for i in population}
    for obj in result.objects:
        if obj.matched_sku and obj.matched_sku not in valid:
            obj.notes = (obj.notes + f" (model suggested unknown SKU {obj.matched_sku})").strip()
            obj.matched_sku = None
    return result
