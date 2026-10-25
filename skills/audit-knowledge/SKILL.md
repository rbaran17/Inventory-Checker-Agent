---
name: audit-knowledge
description: PLACEHOLDER for the firm's audit knowledge skill (audit methodology used to ground the inventory observation agents). Replace this folder's contents with the real skill.
placeholder: true
---

# Audit knowledge skill (placeholder)

This folder is the slot for the firm's audit knowledge skill. Until it is filled, the agents fall back to
the bundled stand-in (`src/inventory_checker/knowledge/standin/`) and label every citation as a placeholder.

## To add the real skill

1. Replace this `SKILL.md` with the skill's own `SKILL.md`. Remove the `placeholder: true` line, or the
   agents keep ignoring this folder.
2. Put the methodology content in `references/` as Markdown files, one section per heading. See
   `references/_FORMAT.md`.
3. Restart the service. With `AUDIT_KNOWLEDGE_SOURCE` unset (or `auto`), the agents now search this folder.

If the skill is a search service rather than files, leave this folder as it is and set
`AUDIT_KNOWLEDGE_SOURCE=http:https://<host>/search` instead (see README, "Audit knowledge").
