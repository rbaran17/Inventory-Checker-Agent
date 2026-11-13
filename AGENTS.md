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
