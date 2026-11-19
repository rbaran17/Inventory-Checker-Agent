# Changelog

## Unreleased

- Security: Python 3.11 is now the minimum. Python 3.10 is end-of-life and can only install Pillow 12, which has
  open advisories and no fix; photos are decoded with Pillow. Minimum versions raised for `fastapi` (patched
  Starlette), `python-multipart`, `pillow` and `mcp`, so an existing install can no longer keep a vulnerable one.
- Security: the access-key check now looks at the path the router serves. It read the path from the rebuilt
  request URL: with an older Starlette, `Host: a/healthz?` made any request look like the health check and
  be served without a key, and on current versions `/healthz%3Fx` got past the check before ending in a 404.
- Security: `X-Forwarded-For` is believed only from the addresses in the new `FORWARDED_ALLOW_IPS` (default:
  this machine; Docker Compose: the tunnel container, which now has a fixed address on a small private
  network). It was believed from anyone, so a client could sidestep the wrong-key limit, lock another address
  out, or make the limiter keep long strings. Addresses are validated and IPv6 is counted per /64. **If your
  own proxy runs on another machine or in another container, set `FORWARDED_ALLOW_IPS`** (see
  `docs/deployment.md`).
- Security: wrong session cookies count towards the wrong-key limit. **Changed:** a blocked address now gets
  429 for signed-in pages too until the five minutes are over; sessions are kept.
- Security: a key set with `APP_ACCESS_KEY` must have at least 16 characters; the server exits with a setup
  message otherwise. Generated keys are unchanged.
- Security: videos are read only as MP4/MOV, Matroska/WebM or AVI and only from the file itself, with PyAV and
  with ffmpeg. A DASH or HLS playlist named `.mp4` made ffmpeg fetch the URLs in it or decode another local
  video. Photos are decoded only as JPEG, PNG, WebP, BMP, TIFF or GIF, and refused above 100 megapixels
  before decoding (Pillow alone stopped at 200). Folder scans skip links that lead out of the folder.
- Security: `--max-scans` / `max_scans` now stop the reading, not only the scanning: all frames of all files
  used to be decoded and kept first. **Changed:** the note in `check` and `skipped` in MCP `check_media`
  count the files left out, not scans; `check_media` prepares at most 200 scans per call; `--every` /
  `every_seconds` must be 0.1 to 3,600 and `segment_frames` 1 to 6; the ffmpeg fallback has a 10-minute
  timeout and a frame limit, and no longer shares the server's stdin.
- Security: a listing may hold 4,000,000 characters of text in total (each item counts its cells and its
  column names), and an Excel file's cell formats (2 MB), shared text table (8 MB), other housekeeping
  parts (2 MB each) and any other single XML part such as a sheet (16 MB) are capped. A listing inside the row, column and cell limits could make every page
  refresh 443 MB, and a 183 KB upload could take over 20 seconds and 1 GB or more of memory to open.
- Security: the CSV export also neutralises listing column names (they were written as-is), and a formula
  after a semicolon, tab or line break inside a value, which Excel with a semicolon list separator treats
  as the start of a cell.
- The Excel export no longer fails when a label, note or listing cell holds a control character; such
  characters are removed, also when a listing is read.
- Security: scan images kept in memory are capped per tab (`MAX_FRAME_STORE_BYTES`, default 256 MB). Past
  it the oldest scans lose their images; their results, the count and the exports are unaffected.
- Security: every response carries `Cache-Control: no-store` (stored frames keep `private, no-cache`) and
  `X-Content-Type-Options: nosniff`.
- Security: the sign-in redirect that drops the key from the address bar can no longer lead to another site
  (`//host/?key=<valid key>` did).
- Release workflow: read-only token by default, write permissions per job, no credentials left in checkouts.
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
