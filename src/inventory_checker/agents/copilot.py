"""Observer copilot: a conversational MAF agent over one observation session.

Read tools run freely. Every tool that writes a record is declared with
``approval_mode="always_require"``, so the framework pauses and returns an approval request
that the observer must accept in the app before anything is saved. Values written that way
are marked ``accepted`` in the record's field provenance.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from agent_framework import Agent, tool
from pydantic import Field

from ..knowledge import GROUNDING_RULES, AuditKnowledgeSource, GuidanceSnippet, build_guidance_tool
from ..models import FieldSource, RecordType
from ..service import ObservationService, ServiceError

INSTRUCTIONS = """You are the observation copilot for an external auditor attending a client's
physical inventory count (AS 2510 / AU-C 501 / ISA 501). The client counts; the auditor observes
and test-counts. You help the auditor keep complete, well-evidenced records.

- Use the read tools to answer questions about the session, the listing, the sample, coverage and
  open items. Quote record ids and SKUs exactly.
- When the auditor dictates an observation, call the matching record_* tool with the values they
  gave. Never invent counts, tag numbers or document numbers. Ask if something is missing.
- Writes need the auditor's approval in the app; say that the draft is waiting for approval.
- Point out unexplained differences over tolerance, counts with no evidence and sampled items not
  yet counted. Keep answers short: the auditor is on a warehouse floor."""


def build_observer_copilot(
    client: Any, service: ObservationService, session_id: str, user: str, *,
    knowledge: AuditKnowledgeSource | None = None, citations: list[GuidanceSnippet] | None = None,
) -> Agent:
    """Build the copilot. With ``knowledge``, it can search audit methodology; hits go to ``citations``."""
    def _dump(obj: Any) -> str:
        return json.dumps(obj, default=str)

    def _create(rtype: RecordType, data: dict[str, Any]) -> str:
        sources = {k: FieldSource.ACCEPTED for k, v in data.items() if v not in (None, "", [])}
        try:
            rec = service.create_record(user, session_id, rtype, data, field_sources=sources)
        except ServiceError as exc:
            return f"Not saved: {exc}"
        return _dump({"saved": rec.id, "data": rec.data})

    # ------------------------------------------------------------------ read tools
    @tool
    def get_session_summary() -> str:
        """Session details, record counts, evidence count, paired phones and open items."""
        return _dump(service.summary(session_id))

    @tool
    def lookup_listing_item(sku: Annotated[str, Field(description="SKU to look up")]) -> str:
        """Look up an item in the client's perpetual listing (description, bin, UOM, client quantity)."""
        item = service.db.get_listing_item(session_id, sku)
        return _dump(item.model_dump()) if item else f"SKU {sku} is not in the listing"

    @tool
    def list_sample(only_uncounted: bool = True) -> str:
        """Sheet-to-floor sample items and their bin locations."""
        counted = {r.data["sku"] for r in service.records(session_id, RecordType.TEST_COUNT)
                   if r.data.get("direction") == "sheet_to_floor"}
        items = [i.model_dump() for i in service.db.list_listing(session_id)
                 if i.sampled and not (only_uncounted and i.sku in counted)]
        return _dump(items)

    @tool
    def get_coverage() -> str:
        """Counts, observations and captures per zone, to spot gaps before leaving."""
        return _dump(service.coverage(session_id))

    @tool
    def list_open_items() -> str:
        """Items that must be resolved or explained before the session can close."""
        return _dump([i.model_dump() for i in service.open_items(session_id)])

    @tool
    def list_records(record_type: Literal["test_count", "procedure_observation", "condition_note",
                                          "cutoff_document", "tag_control"] | None = None) -> str:
        """Current version of each record, optionally filtered by type."""
        return _dump([r.model_dump(mode="json") for r in service.records(session_id, record_type)])

    # ------------------------------------------------------------------ write tools (need approval)
    @tool(approval_mode="always_require")
    def record_test_count(
        sku: str,
        auditor_count: Annotated[float, Field(description="Quantity the auditor counted")],
        direction: Literal["floor_to_sheet", "sheet_to_floor"],
        zone: str | None = None,
        tag_no: str | None = None,
        explanation: str | None = None,
    ) -> str:
        """Draft a test count. The client count is filled from the listing and the difference is computed."""
        return _create(RecordType.TEST_COUNT, {"sku": sku, "auditor_count": auditor_count, "direction": direction,
                                               "zone": zone, "tag_no": tag_no, "explanation": explanation})

    @tool(approval_mode="always_require")
    def record_procedure_observation(
        area: Literal["instructions", "segregation", "movement_freeze", "tag_control"],
        compliant: Literal["yes", "no", "n/a"],
        note: str = "",
    ) -> str:
        """Draft an observation of whether the client follows its count instructions."""
        return _create(RecordType.PROCEDURE, {"area": area, "compliant": compliant, "note": note})

    @tool(approval_mode="always_require")
    def record_condition_note(
        condition: Literal["damaged", "obsolete", "slow_moving", "consigned"],
        zone: str | None = None,
        sku: str | None = None,
        estimated_quantity: float | None = None,
        note: str = "",
    ) -> str:
        """Draft a condition note (feeds valuation and obsolescence work)."""
        return _create(RecordType.CONDITION, {"condition": condition, "zone": zone, "sku": sku,
                                              "estimated_quantity": estimated_quantity, "note": note})

    @tool(approval_mode="always_require")
    def record_cutoff_document(
        type: Literal["receiving", "shipping"],
        document_no: str,
        date: Annotated[str, Field(description="Document date, YYYY-MM-DD")],
        last_before_count: bool = True,
    ) -> str:
        """Draft a cutoff document record (last receiving / shipping document before the count)."""
        return _create(RecordType.CUTOFF, {"type": type, "document_no": document_no, "date": date,
                                           "last_before_count": last_before_count})

    @tool(approval_mode="always_require")
    def record_tag_control(
        range_start: int,
        range_end: int,
        used: list[str] | None = None,
        unused: list[str] | None = None,
        voided: list[str] | None = None,
    ) -> str:
        """Draft a tag control record: issued range and used / unused / voided sub-ranges."""
        return _create(RecordType.TAG_CONTROL, {"range_start": range_start, "range_end": range_end,
                                                "used": used or [], "unused": unused, "voided": voided or []})

    tools = [get_session_summary, lookup_listing_item, list_sample, get_coverage, list_open_items, list_records,
             record_test_count, record_procedure_observation, record_condition_note, record_cutoff_document,
             record_tag_control]
    instructions = INSTRUCTIONS
    if knowledge is not None:
        tools.append(build_guidance_tool(knowledge, log=citations))
        instructions += "\n\n" + GROUNDING_RULES
    else:
        instructions += ("\n\nNo audit methodology source is connected. Do not answer methodology questions from "
                         "memory; refer the auditor to the engagement lead or firm methodology.")
    return Agent(
        client,
        instructions,
        name="observer_copilot",
        description="Helps the observer record and check physical inventory observation evidence.",
        tools=tools,
    )
