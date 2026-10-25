"""AI assist: reads tag numbers, SKUs and lot codes from a photo and suggests a count.

Output is a draft only (PRD: "AI suggests counts and reads tags; the auditor confirms each
record"). It is stored as a Suggestion and never written into a record by this agent.
"""

from __future__ import annotations

from typing import Any, Literal

from agent_framework import Agent, Content, Message
from pydantic import BaseModel, Field

INSTRUCTIONS = """You assist an external auditor observing a client's physical inventory count.
You receive one photo taken in a warehouse. Extract only what is clearly legible.

Rules:
- Never guess. If a value is not clearly readable, leave it null and say why in `notes`.
- `tag_numbers`: pre-numbered count tags visible in the frame.
- `skus` and `lot_codes`: product identifiers printed on labels.
- `bin_code`: a bin / location label if one is visible.
- `suggested_count`: only for a regular, fully visible countable layout (for example cases on a
  pallet face arranged in a grid). Explain the arithmetic in `count_basis` (e.g. "6 wide x 4 high
  x 3 deep visible layers"). Otherwise null.
- Flag `blurry` or `glare` when they would stop a reviewer reading the label.
- `confidence` is your overall confidence in the extract, 0 to 1.
Your output is a draft for a person to confirm. Do not state conclusions about the count's accuracy."""


class EvidenceExtract(BaseModel):
    tag_numbers: list[str] = Field(default_factory=list)
    skus: list[str] = Field(default_factory=list)
    lot_codes: list[str] = Field(default_factory=list)
    bin_code: str | None = None
    uom_hint: str | None = None
    suggested_count: float | None = None
    count_basis: str | None = None
    blurry: bool = False
    glare: bool = False
    quality: Literal["good", "usable", "poor"] = "usable"
    confidence: float = Field(0.0, ge=0, le=1)
    notes: str = ""


def build_evidence_reader(client: Any) -> Agent:
    return Agent(client, INSTRUCTIONS, name="evidence_reader",
                 description="Reads tags, SKUs and countable layouts from inventory photos (drafts only).")


async def read_evidence(client: Any, image: bytes, media_type: str, context: str = "") -> EvidenceExtract:
    agent = build_evidence_reader(client)
    prompt = "Extract the fields from this inventory photo."
    if context:
        prompt += f"\nContext from the observer (may help disambiguate): {context}"
    message = Message(role="user", contents=[Content.from_text(prompt), Content.from_data(image, media_type)])
    response = await agent.run(message, options={"response_format": EvidenceExtract})
    if response.value is not None:
        return response.value
    return EvidenceExtract.model_validate_json(response.text)
