"""Audit knowledge: grounding the agents in audit methodology.

Agents never state methodology from memory. They call ``search_audit_guidance`` and cite the
snippets it returns. The content comes from a pluggable ``AuditKnowledgeSource``:

- ``SkillFolderSource``: a skill folder (``SKILL.md`` plus ``references/*.md``), searched locally.
  The bundled ``standin/`` folder is a placeholder with paraphrased summaries, not firm methodology.
- ``HttpKnowledgeSource``: a search service (for example the firm's audit knowledge skill exposed
  over REST, or an Azure AI Search front end) that implements the small JSON contract below.

Pick one with ``AUDIT_KNOWLEDGE_SOURCE``:

- unset or ``auto``: the firm's skill in ``skills/audit-knowledge/`` (or ``AUDIT_KNOWLEDGE_DIR``) once it
  has been added; until then (its SKILL.md says ``placeholder: true``) the bundled stand-in
- ``standin``: the bundled stand-in
- ``dir:/path/to/skill``: a skill folder
- ``http:https://host/search``: a search service (``AUDIT_KNOWLEDGE_TOKEN`` adds a bearer token)
- ``none``: grounding off
"""

from .sources import (
    AuditKnowledgeSource,
    GuidanceSnippet,
    HttpKnowledgeSource,
    SkillFolderSource,
    audit_skill_dir,
    is_placeholder,
    knowledge_from_env,
    standin_source,
)
from .tool import GROUNDING_RULES, build_guidance_tool, check_citations, cited_ids, format_citations

__all__ = [
    "GROUNDING_RULES",
    "AuditKnowledgeSource",
    "GuidanceSnippet",
    "HttpKnowledgeSource",
    "SkillFolderSource",
    "audit_skill_dir",
    "build_guidance_tool",
    "check_citations",
    "cited_ids",
    "format_citations",
    "is_placeholder",
    "knowledge_from_env",
    "standin_source",
]
