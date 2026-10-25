"""Session planning assistant, grounded in audit methodology.

Suggests the procedures to observe, the sheet-to-floor sampling approach and the difference
tolerance for a session, citing the methodology snippets it relied on. Suggestions are drafts:
nothing changes until the lead applies them (``ObservationService.update_plan``).
Without a chat client it returns a deterministic checklist built from the same guidance.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent_framework import Agent
from pydantic import BaseModel, Field

from ..knowledge import GROUNDING_RULES, AuditKnowledgeSource, GuidanceSnippet, build_guidance_tool
from ..service import ObservationService

log = logging.getLogger(__name__)

PLANNING_QUERIES: list[tuple[str, str | None]] = [
    ("planning the inventory observation", "planning"),
    ("evaluate count instructions tag control segregation movement", "procedures"),
    ("selecting items for test counts high value sampling", "sampling"),
    ("tolerance threshold count differences", "planning"),
    ("count at date other than period end roll forward", "planning"),
    ("inventory held by third parties consignment", "third_party"),
]

INSTRUCTIONS = """You help an engagement lead plan a physical inventory observation.
You get facts about the session and the client's perpetual listing, plus methodology snippets.
Suggest: the count procedures to observe, the sheet-to-floor sampling approach (random sample size and
a value threshold above which every item is counted), the test count difference tolerance, and other
planning checks. Base every suggestion on the snippets (search for more with search_audit_guidance if
needed) and put the snippet ids you used in `citations`. Numbers you suggest for sample size, threshold
or tolerance are illustrations computed from the listing facts; say in the rationale that they are an
engagement team judgement under firm methodology. List anything the methodology leaves open in
`open_questions`."""


class PlanItem(BaseModel):
    item: str
    rationale: str = ""
    citations: list[str] = Field(default_factory=list)


class SamplingSuggestion(BaseModel):
    approach: str
    random_sample_size: int | None = None
    value_threshold: float | None = None
    rationale: str = ""
    citations: list[str] = Field(default_factory=list)


class ToleranceSuggestion(BaseModel):
    units: float | None = None
    pct: float | None = None
    rationale: str = ""
    citations: list[str] = Field(default_factory=list)


class PlanSuggestion(BaseModel):
    procedures_to_observe: list[PlanItem] = Field(default_factory=list)
    sampling: SamplingSuggestion
    tolerance: ToleranceSuggestion
    planning_checks: list[PlanItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def listing_facts(service: ObservationService, session_id: str) -> dict[str, Any]:
    s = service.get_session(session_id)
    items = sorted(service.db.list_listing(session_id), key=lambda i: -i.extended_value)
    total = sum(i.extended_value for i in items)
    facts: dict[str, Any] = {
        "client": s.client, "location": s.location, "count_date": s.count_date,
        "zones": [z.code for z in service.db.list_zones(session_id)],
        "current_plan": {"sample_size": s.sample_size, "sample_value_threshold": s.sample_value_threshold,
                         "tolerance_units": s.tolerance_units, "tolerance_pct": s.tolerance_pct},
        "listing_items": len(items), "listing_value": round(total, 2),
        "top_items": [{"sku": i.sku, "description": i.description, "value": round(i.extended_value, 2)}
                      for i in items[:10]],
    }
    # Value coverage if every item above a few candidate thresholds is counted
    coverage = []
    for frac in (0.5, 0.25, 0.1):
        if not items:
            break
        threshold = round(items[0].extended_value * frac, -2) or items[0].extended_value * frac
        over = [i for i in items if i.extended_value >= threshold]
        coverage.append({"threshold": threshold, "items": len(over),
                         "value_covered_pct": round(100 * sum(i.extended_value for i in over) / total, 1) if total else 0})
    facts["threshold_coverage"] = coverage
    return facts


def planning_guidance(knowledge: AuditKnowledgeSource) -> list[GuidanceSnippet]:
    found: dict[str, GuidanceSnippet] = {}
    for query, topic in PLANNING_QUERIES:
        try:
            hits = knowledge.search(query, topic=topic, top_k=2) or knowledge.search(query, top_k=2)
        except Exception as exc:  # noqa: BLE001 - a failing knowledge service must not stop this step
            log.warning("Audit knowledge search failed for %r: %s", query, exc)
            continue
        for h in hits:
            found.setdefault(h.id, h)
    return list(found.values())


def _clean(plan: PlanSuggestion, allowed: set[str]) -> tuple[PlanSuggestion, list[str]]:
    """Drop citations to snippets that were never retrieved; report them."""
    dropped: list[str] = []

    def keep(ids: list[str]) -> list[str]:
        out = []
        for c in ids:
            c = re.sub(r"^\[?G:|\]$", "", c.strip())
            (out if c in allowed else dropped).append(c)
        return out

    for it in plan.procedures_to_observe + plan.planning_checks:
        it.citations = keep(it.citations)
    plan.sampling.citations = keep(plan.sampling.citations)
    plan.tolerance.citations = keep(plan.tolerance.citations)
    return plan, dropped


def _deterministic(facts: dict[str, Any], knowledge: AuditKnowledgeSource | None,
                   guidance: list[GuidanceSnippet]) -> PlanSuggestion:
    """Checklist without a model. Each item runs its own guidance search and cites the top hit."""

    def cite(query: str, topic: str | None) -> list[str]:
        if knowledge is None:
            return []
        try:
            hits = knowledge.search(query, topic=topic, top_k=1) or knowledge.search(query, top_k=1)
        except Exception as exc:  # noqa: BLE001
            log.warning("Audit knowledge search failed for %r: %s", query, exc)
            return []
        for h in hits:
            if h.id not in {g.id for g in guidance}:
                guidance.append(h)
        return [h.id for h in hits]

    cov = facts["threshold_coverage"]
    best = next((c for c in cov if c["value_covered_pct"] >= 50), cov[-1] if cov else None)
    return PlanSuggestion(
        procedures_to_observe=[
            PlanItem(item="Review the client's count instructions before the count",
                     citations=cite("evaluating count instructions", "procedures")),
            PlanItem(item="Observe tag control, segregation of damaged/consigned goods and the movement freeze",
                     citations=cite("movement freeze segregation", "procedures")),
            PlanItem(item="Perform floor-to-sheet and sheet-to-floor test counts",
                     citations=cite("test counts both directions floor sheet", "test_counts")),
            PlanItem(item="Record the last receiving and shipping documents",
                     citations=cite("receiving shipping cutoff", "cutoff")),
            PlanItem(item="Record tag ranges used, unused and voided", citations=cite("tag control", "tag_control")),
            PlanItem(item="Inspect and note damaged, obsolete or slow-moving items",
                     citations=cite("inspecting condition damaged obsolete", "condition")),
        ],
        sampling=SamplingSuggestion(
            approach="Count every item above a value threshold and select the rest at random",
            value_threshold=best["threshold"] if best else None,
            rationale=(f"Illustration from the listing: counting the {best['items']} item(s) at or above "
                       f"{best['threshold']:,.0f} covers {best['value_covered_pct']}% of listed value. Sample size "
                       "and threshold are an engagement team judgement under firm methodology.") if best else
                      "No listing imported yet.",
            citations=cite("selecting items for test counts high value", "sampling")),
        tolerance=ToleranceSuggestion(
            rationale="Set by the engagement team from materiality and the nature of the items; document the basis.",
            citations=cite("setting tolerance count differences", "planning")),
        planning_checks=[PlanItem(item="Confirm timing, locations, observers, specialists and the client's agreement "
                                       "to photograph the count",
                                  citations=cite("planning the inventory observation", "planning"))],
        open_questions=["Which tolerance and sample size does firm methodology require for this engagement?"],
    )


async def suggest_plan(service: ObservationService, session_id: str, knowledge: AuditKnowledgeSource | None,
                       client: Any | None = None) -> dict[str, Any]:
    facts = listing_facts(service, session_id)
    guidance = planning_guidance(knowledge) if knowledge is not None else []
    dropped: list[str] = []
    if client is None:
        plan = _deterministic(facts, knowledge, guidance)
        generated_by = "checklist"
    else:
        log: list[GuidanceSnippet] = list(guidance)
        tools = [build_guidance_tool(knowledge, log=log)] if knowledge is not None else []
        agent = Agent(client, INSTRUCTIONS + "\n\n" + GROUNDING_RULES, name="planning_assistant", tools=tools)
        resp = await agent.run(json.dumps({"facts": facts, "guidance": [g.to_dict() for g in guidance]}, default=str),
                               options={"response_format": PlanSuggestion})
        plan = resp.value if resp.value is not None else PlanSuggestion.model_validate_json(resp.text)
        guidance = log
        plan, dropped = _clean(plan, {g.id for g in guidance})
        generated_by = "ai_draft"
    used = {c for it in plan.procedures_to_observe + plan.planning_checks for c in it.citations}
    used |= set(plan.sampling.citations) | set(plan.tolerance.citations)
    service.db.append_audit("ai-assist" if client else "planning-checklist", "plan.suggest", session_id=session_id,
                            details={"citations": sorted(used), "dropped_citations": dropped})
    return {"generated_by": generated_by, "plan": plan.model_dump(), "facts": facts,
            "guidance": [g.to_dict() for g in guidance if g.id in used] or [g.to_dict() for g in guidance],
            "dropped_citations": dropped, "knowledge_source": getattr(knowledge, "name", None)}
