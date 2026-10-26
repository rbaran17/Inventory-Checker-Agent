# Inventory-Checker-Agent

Physical inventory observation agent for audit teams, built on **Microsoft Agent Framework (MAF)**.

Auditors observing a client's count pair their phone to the observation app by scanning a QR code.
The phone streams photos, bursts and short clips straight into structured observation records
(test counts, procedure observations, condition notes, cutoff documents, tag control). Each capture is
hashed on the phone, re-verified by the server and kept in write-once storage. At close, a MAF workflow
checks open items, locks the session and produces a **structured Excel workbook**, a test-count schedule
CSV, an observation memo draft and an evidence package with a SHA-256 manifest.

Source PRD: *Physical Inventory Observation – Phone Camera Capture: Tech Spec*.

## Architecture

```
 On site                                    Firm cloud
 ┌──────────────────────┐  HTTPS   ┌───────────────────────┐      ┌──────────────────────┐
 │ Phone capture page   │────────▶ │ Ingestion service     │────▶ │ Evidence store       │
 │ /pair (no app install)│         │ SSO, re-check SHA-256 │      │ write-once (WORM)    │
 │ photo/burst/clip,     │         │ stamp receipt time    │      └──────────────────────┘
 │ SHA-256, offline queue│         └───────────┬───────────┘
 └──────────▲───────────┘                      ▼
            │ scan QR            ┌───────────────────────┐      ┌──────────────────────┐
 ┌──────────┴───────────┐ HTTPS  │ Observation service   │◀────▶│ AI assist (MAF)      │
 │ Observation app  /   │◀──────▶│ versioned records,    │      │ drafts only          │
 │ laptop or tablet     │        │ hash-chained audit    │      └──────────────────────┘
 └──────────────────────┘        └───────────┬───────────┘
                                             ▼
                                 ┌───────────────────────┐
                                 │ Close-out workflow    │  Excel / CSV / memo /
                                 │ (MAF Workflow)        │  evidence + hash manifest
                                 └───────────────────────┘
```

| Module | What it does |
|---|---|
| `models.py` | PRD data model: Session, Zone, TestCount, ProcedureObservation, ConditionNote, CutoffDocument, TagControl, Evidence, ReviewEvent, plus field provenance (`typed` / `accepted` / `edited` / `imported`) |
| `store.py` | SQLite metadata with DB triggers that make evidence, record versions and the audit trail immutable; `LocalWormEvidenceStore` (refuses overwrite/delete); hash-chained audit log with `verify_audit_chain()` |
| `service.py` | Business rules: planning, sampling, pairing, ingestion, records and corrections, coverage, open items, close/lock, review, sign-off |
| `agents/evidence_reader.py` | MAF `Agent` with structured output (`EvidenceExtract`): reads tags, SKUs, lot codes, bin codes, suggests counts for regular layouts, flags blur/glare |
| `agents/copilot.py` | MAF `Agent` with tools over the session. Read tools run freely; every write tool uses `approval_mode="always_require"`, so nothing is saved until the observer approves |
| `agents/memo.py` | Deterministic memo body traced to record ids, plus an optional AI-drafted summary, labelled as such |
| `agents/closeout.py` | MAF `Workflow`: `OpenItemsGate → MemoDrafter → ExportPackager` |
| `agents/planning.py` | Planning assistant: procedures, sampling and tolerance suggestions grounded in methodology; the lead applies them |
| `knowledge/` | Audit knowledge interface, the `search_audit_guidance` MAF tool, citation checks, and a placeholder `standin/` skill folder |
| `export.py` | Excel workbook (Summary, Test Counts with amber rows over tolerance, Procedures, Condition Notes, Cutoff, Tag Control, Evidence Manifest, Close Explanations), CSV schedule, evidence ZIP |
| `api.py` | FastAPI app: JSON API, `/` observation app, `/pair` phone capture page |
| `static/` | `capture.html` (phone) and `app.html` (laptop), plain HTML/JS with no build step |

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # add ,azure for Entra ID sign-in to Azure OpenAI
cp .env.example .env             # set AZURE_OPENAI_* to turn on AI assist (optional)

inventory-checker demo           # seeds a session with zones and samples/perpetual_listing.csv
inventory-checker serve --port 8443 --certfile cert.pem --keyfile key.pem
```

Open `https://<host>:8443/` on the laptop and sign in as `lead@firm.example`. Choose the session, press
**Pair phone** and scan the QR code with the phone. Phones only allow camera access over HTTPS. Use a
certificate the phone trusts, and set `PUBLIC_BASE_URL` to the hostname the phone will reach.

Run the tests with `pytest`. The MAF agents are tested with a scripted chat client, so no model or
network is needed.

### AI configuration

AI assist is optional. With nothing configured, the app works and the AI buttons and copilot are hidden.
Choose the model provider with `AI_PROVIDER`. If it is unset, the first provider configured in this order is used:

| `AI_PROVIDER` | Settings | Notes |
|---|---|---|
| `claude` | `ANTHROPIC_API_KEY`; optional `CLAUDE_MODEL` (default `claude-opus-5-5`), `CLAUDE_EFFORT` (default `medium`), `CLAUDE_MAX_TOKENS` (default 16000) | Claude API via MAF's `agent-framework-anthropic`. Server-side refusal fallback is on |
| `claude-foundry` | `ANTHROPIC_FOUNDRY_RESOURCE`, `ANTHROPIC_FOUNDRY_API_KEY` | Claude in the firm's Azure tenant (Microsoft Foundry). No server-side fallback there |
| `azure` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`; optional `AZURE_OPENAI_API_KEY` (else Entra ID), `AZURE_OPENAI_API_VERSION` | Azure OpenAI |
| `openai` | `OPENAI_API_KEY`, `OPENAI_MODEL` | Local development only |
| `none` | | AI off |

Check which model is active in the observation app header, or with `GET /api/me`.

### Audit knowledge (methodology grounding)

The agents never state methodology from memory. They call the `search_audit_guidance` tool and cite each
snippet they rely on as `[G:<id>]`. Citations to snippets that were never retrieved are removed and reported.

| Where | How it is grounded |
|---|---|
| Observer copilot | Searches guidance when the auditor asks a methodology question; the reply lists the sources it cited |
| Memo drafting | Looks up guidance for each memo section that has records and adds a **Methodology references** section; the AI summary may cite only those snippets |
| Session planning | **Suggest plan from methodology** (lead only, in *Listing & sample*) proposes procedures, sampling and tolerance with citations. Nothing changes until the lead applies it, and the change is audited |
| Any user | Guidance search box in *Listing & sample*, or `GET /api/guidance?q=` |

**Adding the firm's audit knowledge skill.** The repo has an empty slot for it at
[`skills/audit-knowledge/`](skills/audit-knowledge/SKILL.md). Its `SKILL.md` is marked `placeholder: true`.
Until the real skill replaces it, the agents use the bundled stand-in. To add the skill:

1. Replace `skills/audit-knowledge/SKILL.md` with the skill's own, without the `placeholder: true` line.
2. Put the methodology Markdown in `skills/audit-knowledge/references/`. The format is in `references/_FORMAT.md`.
3. Restart the service. No configuration change is needed.

To use a different source, set `AUDIT_KNOWLEDGE_SOURCE`:

| Value | Source |
|---|---|
| unset / `auto` | `skills/audit-knowledge/` (or `AUDIT_KNOWLEDGE_DIR`) once the skill is added; until then the stand-in |
| `standin` | Bundled placeholder (`knowledge/standin/`). Paraphrased summaries, flagged in the UI and memo as *placeholder, verify against firm methodology* |
| `dir:/path/to/skill` | A skill folder: `SKILL.md` plus `references/**/*.md`, split by heading. Optional `> refs:` and `> topics:` lines under a heading become citation metadata |
| `http:https://host/search` | A search service. `POST {"query","top_k","topic","domain":"inventory"}` returns `{"results":[{"id","title","text","source","refs","topics","url","score"}]}`. `AUDIT_KNOWLEDGE_TOKEN` is sent as a bearer token |
| `none` | Grounding off. The copilot is told to send methodology questions to the engagement lead |

Any other backend (for example an MCP server or Azure AI Search) only needs a class with
`name` and `search(query, *, topic=None, top_k=5) -> list[GuidanceSnippet]`, passed to `create_app(knowledge=...)`.

## Workflow coverage (PRD → implementation)

| PRD step | Where |
|---|---|
| Plan, with methodology suggestions | `POST /api/sessions/{id}/plan-suggestions`, `POST /api/sessions/{id}/plan` |
| Plan: session, zones, listing, high-value sample | `POST /api/sessions`, `/zones`, `/listing`, `/sample` (random + all items over the value threshold) |
| Pair: one-time QR, 5 min, single use, one camera per observer | `create_pairing_token` / `redeem_pairing`; phone shows session, location and observer; laptop shows phone and battery; one-tap unpair on either side |
| Capture: photo, 3-frame burst keeping the sharpest, 5–30 s clip | `capture.html` (Laplacian-variance sharpness, MediaRecorder, steadiness indicator, Wake Lock, BarcodeDetector for bin codes) |
| Per-capture metadata, two clocks | SHA-256 on the phone (`crypto.subtle`), server recompute and reject on mismatch, device and receipt time, clock gap shown in the tray |
| Poor connectivity | IndexedDB queue, visible queue count, idempotent retry by hash; queued uploads block close |
| Live evidence tray, drag to move | `app.html` polling every 3 s; captures attach to the record open on the observer's laptop; moves are audited |
| Test count form, tolerance | Client count filled from the listing; difference computed server-side, never typed; amber when over tolerance |
| AI assist, suggestions only | `/api/evidence/{id}/suggest` stores a `Suggestion`; the form shows drafts; provenance kept per field |
| Coverage map | `/coverage` per zone |
| Open-items check at close | Counts without evidence, unexplained differences, tag control missing or unused tags not recorded, sampled items not counted, queried records, queued uploads |
| Close / lock / review / sign-off | Closed sessions take no captures; review approve/query/reject (query reopens); reviewed sessions take only follow-up notes |
| Export | Excel workbook, CSV schedule, memo draft, evidence ZIP with `manifest.json` and `SHA256SUMS.csv`, records with full version history, audit trail |

## Known gaps (pilot build)

- **SSO/MFA.** `AUTH_MODE=dev` trusts an `X-User` header or a cookie set by the dev sign-in box. In
  production, put the service behind the firm's identity-aware proxy (e.g. Entra ID). The proxy must set
  `TRUSTED_USER_HEADER` and strip any copy of that header sent by clients.
- **WORM storage** is simulated locally (read-only files, no overwrite or delete, DB triggers). Production
  should implement `EvidenceStore` on object storage with retention lock (e.g. Azure Blob immutable
  storage) at the firm's retention period (7 years PCAOB AS 1215, at least 5 years AU-C 230).
- **Face blurring on export** is not implemented. The manifest records `faces_blurred: false`. Blurred
  copies should be derived files linked to the originals, so the original hashes stay valid.
- **Workpaper-system import format** is not yet known (PRD open question). Excel and CSV are produced today.
- The "active record" (where phone captures attach) is held in memory per observer and resets on restart.
- The audit knowledge skill is not added yet (`skills/audit-knowledge/` is a placeholder slot). Until it
  is, citations come from the stand-in and the keyword search is basic. Do not rely on them.
- Default tolerance and who can change it are still open questions. Tolerance is set per session by the lead.
