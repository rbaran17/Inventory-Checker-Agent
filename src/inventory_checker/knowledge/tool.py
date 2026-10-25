"""MAF tool that lets an agent search audit methodology and cite what it used."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from agent_framework import FunctionTool, tool
from pydantic import Field

from .sources import AuditKnowledgeSource, GuidanceSnippet

Topic = Literal["planning", "procedures", "test_counts", "cutoff", "tag_control", "condition", "sampling",
                "documentation", "alternative_procedures", "third_party"]

GROUNDING_RULES = """Audit methodology grounding:
- For any methodology question (what the standards or the firm require, how to perform or document a
  procedure, sample sizes, tolerances), call search_audit_guidance first and answer from its results.
- Cite each guidance snippet you rely on as [G:<id>]. Do not state methodology from memory.
- If the results do not cover the question, say so and suggest the auditor consult the engagement lead
  or the firm's methodology. If a result has "authoritative": false, say it is a placeholder summary that
  must be checked against firm methodology."""


def build_guidance_tool(source: AuditKnowledgeSource, *, log: list[GuidanceSnippet] | None = None) -> FunctionTool:
    """Return a ``search_audit_guidance`` tool. Snippets returned are appended to ``log`` if given."""

    @tool(name="search_audit_guidance")
    def search_audit_guidance(
        query: Annotated[str, Field(description="What to look up, e.g. 'test count direction floor to sheet'")],
        topic: Topic | None = None,
        top_k: Annotated[int, Field(ge=1, le=8)] = 4,
    ) -> str:
        """Search audit methodology (inventory observation guidance) and return citable snippets."""
        try:
            hits = source.search(query, topic=topic, top_k=top_k)
            if not hits and topic:
                hits = source.search(query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - a failing knowledge service must not break the agent
            return json.dumps({"error": f"Audit knowledge search failed: {exc}", "results": []})
        if log is not None:
            log.extend(h for h in hits if h.id not in {x.id for x in log})
        return json.dumps({"source": source.name, "results": [h.to_dict() for h in hits]})

    return search_audit_guidance


def format_citations(snippets: list[GuidanceSnippet]) -> list[str]:
    lines = []
    for s in snippets:
        refs = f" ({'; '.join(s.refs)})" if s.refs else ""
        flag = " - placeholder, verify against firm methodology" if not s.authoritative else ""
        lines.append(f"- [G:{s.id}] {s.title}{refs}, source `{s.source}`{flag}")
    return lines


_CITE = re.compile(r"\[G:([^\]]+)\]")


def cited_ids(text: str) -> list[str]:
    return list(dict.fromkeys(m.strip() for m in _CITE.findall(text or "")))


def check_citations(text: str, allowed: set[str]) -> str:
    """Replace citations to snippets the agent was never given."""
    return _CITE.sub(lambda m: m.group(0) if m.group(1).strip() in allowed else "[citation not found]", text or "")
