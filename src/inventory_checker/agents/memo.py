"""Observation memo draft.

The factual body (procedures, test counts, cutoff, tag control, conditions) is generated
deterministically from the records so every statement traces to a record id. When a chat
client is configured, a MAF agent adds a short overall summary, clearly labelled as an
AI draft for the engagement lead to edit.
"""

from __future__ import annotations

import json
from typing import Any

from agent_framework import Agent

from ..models import RecordType
from ..service import ObservationService

SUMMARY_INSTRUCTIONS = """You draft the overall summary paragraph of an auditor's physical inventory
observation memo (AS 2510 / AU-C 501 / ISA 501). You get the session's records as JSON.
Write 4-6 plain sentences: scope (location, date, what was observed), whether count procedures
were followed, test count results (number performed, number of differences over tolerance and
whether they were explained), cutoff and tag control coverage, and anything the lead should follow
up. Cite record ids in brackets for every exception. Do not conclude on the fairness of the
inventory balance; that is the engagement team's judgement."""


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


async def draft_memo(service: ObservationService, session_id: str, client: Any | None = None) -> str:
    body = memo_body(service, session_id)
    if client is None:
        return body
    records = [r.model_dump(mode="json") for r in service.records(session_id)]
    agent = Agent(client, SUMMARY_INSTRUCTIONS, name="memo_writer")
    resp = await agent.run(json.dumps({"session": service.get_session(session_id).model_dump(mode="json"),
                                       "records": records}, default=str))
    title, rest = body.split("\n", 1)
    return f"{title}\n\n## Summary (AI draft - review and edit before use)\n\n{resp.text.strip()}\n{rest}"
