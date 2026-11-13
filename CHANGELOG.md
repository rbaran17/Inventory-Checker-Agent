# Changelog

## Unreleased

- `inventory-checker check --offline`: run the whole headless flow with a stand-in detector and no API key,
  to check an installation and see the outputs before spending credits. Results are labelled as not real.
- `inventory-checker check --detector module:function`, and detectors in the current folder now load with the
  installed command (`DETECTOR=examples.custom_detector:detect` used to fail with a traceback).
- `check` exits 2 with a plain message when no photos or videos are found or the detector cannot be loaded.
- No-key quick start in the README, `docs/headless.md` and `AGENTS.md`.

## 0.5.0

Headless use (no browser or phone needed):

- `inventory-checker check`: scan photos, videos or folders against a listing and write `.xlsx`/`.csv`/`.json`.
  Includes `--dry-run` (model-call count, no API key needed), `--json` on stdout, `--fail-on` exit codes and a
  `--max-scans` cost guard. Videos become 4-frame segments (PyAV or ffmpeg); photos are rotated using EXIF data
  and downscaled like phone close-ups.
- `inventory-checker mcp`: MCP server (stdio or streamable HTTP) with tools to load a listing, check
  media or an inline image, get results, correct matches, review and export. File access is confined to
  `--allowed-dir`.
- Python API: `from inventory_checker import InventoryCheck`. `as_tools()` exposes it to Microsoft Agent
  Framework agents.
- The web app, CLI, MCP server and API share one scan-building and matching path (`engine.py`).
- New extras: `video`, `mcp`, `all`. The Docker image includes them. Added `docs/headless.md` and `AGENTS.md`.


Security:

- Exports: text starting with `=`, `+`, `-` or `@` (from a label, the listing or a note) is no longer written
  as a live formula. Excel cells are stored as text; CSV cells get a leading apostrophe.
- Listing upload: row, column, cell and unpacked-size limits; `defusedxml` is now a dependency so workbook XML
  is parsed safely; parsing no longer blocks the server.
- Scan IDs are unique across the Engagement and Demo tabs and never reused; removing or correcting a scan
  acts on that scan whichever tab is open. Detected objects have a stable `id`. **API change:**
  `/api/scans/{id}/objects/{object_id}` takes that `id` instead of a list position.
- A scan that finishes after the listing was replaced is rejected instead of being matched to the new listing.
- Stored frames are revalidated by the browser (ETag) instead of being cached for an hour.
- The QR code carries a phone-only key that can scan and remove scans but not read or change the listing,
  export results or manage the session. Session cookies hold a derived value, last 12 hours, and are
  renamed (`ic_session`, `ic_phone`), so open pages must be reopened from the link once after upgrading.
- Access keys are compared safely for any input (a non-ASCII `?key=` used to cause a 500), wrong keys are
  throttled per client address, `?key=` is redacted from the access log, and the key file is created
  owner-only. Generated keys are longer. New `inventory-checker rotate-key` command.
- Requests larger than `MAX_UPLOAD_BYTES` are refused before the body is read. New `MAX_SCANS_PER_MINUTE`
  (default 30) and `MAX_CONCURRENT_SCANS` (default 2).

## 0.4.0

- **Items to be tested listing** replaces population + sampling: upload a CSV or Excel file. Item ID,
  description, expected quantity and location are recognised from common header names; every other column
  (serial number, model number, manufacturer, asset tag, ...) is an attribute to verify. Blank cells mean
  "record what is read".
- The model reads those attributes from labels (it is given attribute names, never expected values). The app
  compares them, ignoring case, spacing and punctuation, flags one-character misreads on long codes as
  *near match*, and lets an attribute match (e.g. serial number) decide which listed item an object is.
- Laptop app rebuilt as a five-step workflow: items to be tested → connect phone → scan & match → review
  exceptions (accept / follow up, with notes) → summary & export (Excel workbook and CSV).
- **Engagement** and **Demo** tabs with separate data; the demo has twelve everyday items with editable
  expected values.
- Phone app rebuilt: full-screen camera, Scan / Close-up modes (close-ups sent at higher resolution for label
  text), latest-result card, Results sheet, remove a whole scan or a single detected item.
- `SAMPLE_FILE` loads a listing at startup (replaces `POPULATION_CSV`).

## 0.3.0

- Model providers through Microsoft Agent Framework: Anthropic Claude (default `claude-haiku-5-5`),
  Azure OpenAI (API key or Entra ID) and OpenAI, chosen with `AI_PROVIDER` or auto-detected.
- One strict, provider-neutral structured-output schema; ranges enforced in code.
- Plug-in detectors (`DETECTOR=module:function`) and provider registry (`register_provider`).
- Container image (non-root, health check) and Docker Compose with a Cloudflare tunnel sidecar whose public
  address the app discovers automatically.
- `POPULATION_CSV` to start with your own population; `/healthz` endpoint.
- CI (lint, tests on Python 3.10-3.13, image build) and release workflow (GHCR image, wheel, sdist).

## 0.2.0

- Proof of concept: client population, sampling, phone-camera video scanning, automatic match to the sample.
