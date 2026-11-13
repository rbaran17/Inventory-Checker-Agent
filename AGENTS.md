# Notes for coding agents

Instructions for AI coding agents (and people) working on this repository.

## Setup and checks

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"
ruff check src tests examples
pytest -q                     # no API keys or network: providers are faked
```

Run both before every commit. CI runs the same on Python 3.10–3.13, then builds the Docker image and
smoke-tests it.

On Windows: `.venv\Scripts\activate` instead of `source .venv/bin/activate`.

## Running a count without a browser or phone

If you are an agent asked to *use* this tool rather than change it, you need one command. Progress goes to
stderr, results as JSON to stdout, and the exit code tells you what happened.

```bash
# 1. Check the installation and see the output shape. No API key, no cost, results are not real.
inventory-checker check --listing examples/items_to_be_tested.csv docs/assets/phone-camera.png --offline --json -q

# 2. See how many model calls a real run would make. No API key, no cost.
inventory-checker check --listing LISTING.xlsx PHOTOS_OR_VIDEOS/ --dry-run

# 3. Real run. Needs ANTHROPIC_API_KEY, or the Azure OpenAI / OpenAI settings (see .env.example).
inventory-checker check --listing LISTING.xlsx PHOTOS_OR_VIDEOS/ --out results.xlsx --json -q --fail-on exception
```

- Exit `0` done, `1` `--fail-on` matched, `2` setup error or every scan failed. The reason is on stderr.
- In the JSON, read `results.summary` for counts by status and `results.items[]` for each item's `status`,
  `counted_qty`, `expected_qty` and `checks`. `errors[]` lists scans that failed.
- Never present `--offline` output as a count: the stand-in does not look at the images.
- Each photo and each 4-frame video segment is one paid model call. `--max-scans` (default 50) caps them.
- To drive a count step by step (correct matches, record reviews), use the MCP server or the Python API.

Full guide: [docs/headless.md](docs/headless.md).

## Layout

| Path | What |
|---|---|
| `src/inventory_checker/sample.py` | Items to be tested listing: parsing (CSV/XLSX), demo, `reconcile`, `compare`, `assess` (pure functions) |
| `src/inventory_checker/detection.py` | MAF agent, prompt, provider-neutral JSON schema, custom `DETECTOR` loader |
| `src/inventory_checker/providers.py` | Anthropic / Azure OpenAI / OpenAI chat clients, provider registry |
| `src/inventory_checker/engine.py` | `InventoryCheck` (headless engine), `build_scan`, `rereconcile`, `detector_from_env` |
| `src/inventory_checker/media.py` | Photos and videos on disk → frames and segments |
| `src/inventory_checker/api.py` | FastAPI web app (laptop and phone), access control in `security.py` |
| `src/inventory_checker/mcp_server.py` | MCP server (`inventory-checker mcp`) |
| `src/inventory_checker/cli.py` | `serve`, `check`, `mcp`, `rotate-key` |
| `src/inventory_checker/export.py` | Excel/CSV export (formula-injection safe) |
| `src/inventory_checker/static/` | `dashboard.html` (laptop), `scan.html` (phone): plain HTML/JS, no build step |

## Rules

- **The model never sees expected attribute values.** Only attribute names go in the prompt; comparison is in
  `sample.compare`. Keep it that way, and keep the test that checks it.
- **The answer schema (`detection.RESPONSE_SCHEMA`) must stay provider-neutral:** every property required,
  `additionalProperties: false` on every object, no numeric limits. `tests/test_detection.py` enforces
  Anthropic's and OpenAI's rules.
- **The web app, CLI, MCP server and Python API share `engine.build_scan` / `rereconcile`.** Change matching in
  one place.
- **Tests never call real model APIs or spend credits.** Use a fake detector or the fake HTTP servers in
  `tests/test_detection.py`.
- **Never commit** API keys, `.env`, certificates, access keys or real client data.
- **MCP tools** raise `ValueError` / `KeyError` for mistakes the caller can fix; `_friendly` turns them into
  readable tool errors. File paths must go through `inside()` (confined to `--allowed-dir`).
- User-facing text calls the uploaded file the **"items to be tested listing"**.
