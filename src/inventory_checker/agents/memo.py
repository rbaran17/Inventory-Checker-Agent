"""Observation memo draft.

The factual body (procedures, test counts, cutoff, tag control, conditions) is generated
deterministically from the records so every statement traces to a record id. When a chat
client is configured, a MAF agent adds a short overall summary, clearly labelled as an
AI draft for the engagement lead to edit.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent_framework import Agent

from ..knowledge import AuditKnowledgeSource, GuidanceSnippet, check_citations, format_citations
from ..models import RecordType
from ..service import ObservationService

log = logging.getLogger(__name__)

SUMMARY_INSTRUCTIONS = """You draft the overall summary paragraph of an auditor's physical inventory
observation memo (AS 2510 / AU-C 501 / ISA 501). You get the session's records as JSON.
Write 4-6 plain sentences: scope (location, date, what was observed), whether count procedures
were followed, test count results (number performed, number of differences over tolerance and
whether they were explained), cutoff and tag control coverage, and anything the lead should follow
up. Cite record ids in brackets for every exception. Do not conclude on the fairness of the
inventory balance; that is the engagement team's judgement.
If methodology guidance snippets are provided, you may cite them as [G:<id>] where a sentence relies on
them. Cite only ids from the provided list. Never state methodology that is not in the snippets."""

# Guidance looked up for each memo section that has content: (record type or None = always, query, topic)
MEMO_GUIDANCE_QUERIES: list[tuple[RecordType | None, str, str | None]] = [
    (None, "attendance at physical inventory count existence condition", "procedures"),
    (RecordType.PROCEDURE, "evaluate count instructions observe performance of count procedures", "procedures"),
    (RecordType.TEST_COUNT, "test counts floor to sheet sheet to floor", "test_counts"),
    (RecordType.TEST_COUNT, "test count differences recount investigate", "test_counts"),
    (RecordType.CUTOFF, "receiving shipping cutoff last documents", "cutoff"),
    (RecordType.TAG_CONTROL, "tag control used unused voided", "tag_control"),
    (RecordType.CONDITION, "inspect condition damaged obsolete slow moving", "condition"),
    (None, "documenting the inventory observation", "documentation"),
]


def _fmt(v: Any) -> str:
    return "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def memo_body(service: ObservationService, session_id: str) -> str:
    s = service.get_session(session_id)
    R = lambda t: service.records(session_id, t)
    out = [
        f"# Physical inventory observation memo: {s.client}, {s.location}",
        "",
        f"- **Engagement:** {s.engagement}",
        f"- **Count date:** {s.count_date}",
        f"- **Observers:** {', '.join(s.observers) or 'none recorded'}",
        f"- **Engagement lead:** {s.lead}",
        f"- **Session status:** {s.status.value}",
        "",
        "## Count procedures observed",
    ]
    procs = R(RecordType.PROCEDURE)
    out += [f"- {r.data['area'].replace('_', ' ').capitalize()}: **{r.data['compliant']}**. {r.data.get('note', '')} "
            f"[{r.id}]" for r in procs] or ["- No procedure observations recorded."]

    counts = R(RecordType.TEST_COUNT)
    over = [r for r in counts if r.data.get("over_tolerance")]
    out += ["", "## Test counts",
            (f"{len(counts)} test count(s) performed "
            f"({sum(1 for r in counts if r.data['direction'] == 'floor_to_sheet')} floor-to-sheet, "
            f"{sum(1 for r in counts if r.data['direction'] == 'sheet_to_floor')} sheet-to-floor); "
            f"{len(over)} difference(s) over the tolerance of {s.tolerance_units:g} units / {s.tolerance_pct:g}%."),
            "", "| SKU | Tag | Client | Auditor | Diff | Explanation | Record |", "|---|---|---|---|---|---|---|"]
    out += [f"| {r.data['sku']} | {_fmt(r.data.get('tag_no'))} | {_fmt(r.data.get('client_count'))} | "
            f"{_fmt(r.data['auditor_count'])} | {_fmt(r.data.get('difference'))} | "
            f"{_fmt(r.data.get('explanation'))} | {r.id} |" for r in over] or ["| (none over tolerance) |||||||"]

    out += ["", "## Cutoff"]
    out += [f"- Last {r.data['type']} document before count: **{r.data['document_no']}** dated {r.data['date']} "
            f"[{r.id}]" for r in R(RecordType.CUTOFF)] or ["- No cutoff documents recorded."]
    out += ["", "## Tag control"]
    out += [f"- Range {r.data['range_start']}-{r.data['range_end']}: used {', '.join(r.data['used']) or '-'}; "
            f"unused {', '.join(r.data['unused']) if r.data['unused'] is not None else 'NOT RECORDED'}; "
            f"voided {', '.join(r.data['voided']) or '-'} [{r.id}]" for r in R(RecordType.TAG_CONTROL)] \
        or ["- No tag control recorded."]
    out += ["", "## Condition of inventory"]
    out += [f"- {r.data['condition'].replace('_', ' ')}: {_fmt(r.data.get('sku')) or 'zone ' + _fmt(r.data.get('zone'))}"
            f", est. qty {_fmt(r.data.get('estimated_quantity'))}. {r.data.get('note', '')} [{r.id}]"
            for r in R(RecordType.CONDITION)] or ["- No condition exceptions noted."]
    if s.close_explanations:
        out += ["", "## Open items explained at close"]
        out += [f"- `{k}`: {v}" for k, v in s.close_explanations.items()]
    ev = service.db.list_evidence(session_id)
    out += ["", "## Evidence",
            (f"{len(ev)} photo(s)/clip(s), each hashed (SHA-256) on the phone, re-verified on receipt and held in "
            "write-once storage. See the evidence package manifest.")]
    return "\n".join(out) + "\n"


def memo_guidance(service: ObservationService, session_id: str, knowledge: AuditKnowledgeSource) -> list[GuidanceSnippet]:
    """Methodology snippets relevant to the sections this memo actually contains."""
    present = {r.type for r in service.records(session_id)}
    found: dict[str, GuidanceSnippet] = {}
    for rtype, query, topic in MEMO_GUIDANCE_QUERIES:
        if rtype is not None and rtype not in present:
            continue
        try:
            hits = knowledge.search(query, topic=topic, top_k=1) or knowledge.search(query, top_k=1)
        except Exception as exc:  # noqa: BLE001 - a failing knowledge service must not stop this step
            log.warning("Audit knowledge search failed for %r: %s", query, exc)
            continue
        for h in hits:
            found.setdefault(h.id, h)
    return list(found.values())


async def draft_memo(service: ObservationService, session_id: str, client: Any | None = None,
                     knowledge: AuditKnowledgeSource | None = None) -> str:
    body = memo_body(service, session_id)
    guidance = memo_guidance(service, session_id, knowledge) if knowledge is not None else []
    if guidance:
        body += "\n## Methodology references\n\n" + "\n".join(format_citations(guidance)) + "\n"
    if client is None:
        return body
    records = [r.model_dump(mode="json") for r in service.records(session_id)]
    agent = Agent(client, SUMMARY_INSTRUCTIONS, name="memo_writer")
    resp = await agent.run(json.dumps({"session": service.get_session(session_id).model_dump(mode="json"),
                                       "records": records, "guidance": [g.to_dict() for g in guidance]},
                                      default=str))
    summary = check_citations(resp.text.strip(), {g.id for g in guidance})
    title, rest = body.split("\n", 1)
    return f"{title}\n\n## Summary (AI draft - review and edit before use)\n\n{summary}\n{rest}"
