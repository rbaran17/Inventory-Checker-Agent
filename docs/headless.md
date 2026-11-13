# Headless use: CLI, MCP server and Python API

The web app (laptop + phone) is one way to run a count. The same engine also runs **without a browser or a
phone**, which makes it easy to automate, to call from another AI agent, or to build into your own software.

| Way | Use it for | Command / import |
|---|---|---|
| **CLI** | Scripts, scheduled jobs, CI, a folder of photos or videos someone sent you | `inventory-checker check` |
| **MCP server** | Letting an AI agent (Claude Code, Claude Desktop, IDE assistants, your own agent) run a count conversationally | `inventory-checker mcp` |
| **Python API** | Embedding in your own Python app or a Microsoft Agent Framework (MAF) workflow | `from inventory_checker import InventoryCheck` |

All three use the same pieces as the web app:
- the same items to be tested listing format;
- the same detection on Anthropic, Azure OpenAI or OpenAI through MAF (see [providers.md](providers.md));
- the same matching and attribute comparison;
- the same Excel and CSV exports.

A photo is treated like a phone **Close-up** (one frame, 2048 px). A video is split into segments like the
phone's **Scan** mode (4 frames, one every second, 1280 px). Costs and results are therefore comparable with
the phone flow: one model call per photo or video segment.

## Install

```bash
pip install -e ".[all]"      # from a clone: CLI + video support + MCP server + Azure sign-in
# or pick extras: .[video] (read video files)  .[mcp] (MCP server)  .[azure] (keyless Azure OpenAI)
export ANTHROPIC_API_KEY=sk-ant-...   # or the Azure OpenAI / OpenAI settings
```

Reading **videos** needs PyAV (the `video` extra) or the `ffmpeg` command on your PATH. Photos need nothing
extra. **HEIC photos** aren't supported: export them as JPEG, or set the iPhone camera to *Most Compatible*.

The Docker image includes all extras:
```bash
docker run --rm -v "$PWD:/work" -w /work --env-file .env ghcr.io/rbaran17/inventory-checker-agent \
  inventory-checker check --listing items.xlsx photos/ --out results.xlsx
```

---

## CLI: `inventory-checker check`

```bash
inventory-checker check --listing items_to_be_tested.xlsx  photos/  aisle3.mp4  \
    --out results.xlsx --out results.json \
    --client "Acme Ltd" --location "Main warehouse" --count-date 2026-12-31 --counter "R. Auditor"
```

| Option | Meaning |
|---|---|
| `--listing FILE` / `--demo` | The items to be tested listing (CSV or .xlsx), or the built-in demo of everyday items |
| `media ...` | Photos, videos, or folders of them. Folders are read one level deep: images and videos only, other files ignored |
| `--out FILE` | `.xlsx`, `.csv` or `.json`. Repeat it to write several |
| `--every SECONDS` | Video: seconds between frames (default 1.0) |
| `--segment-frames N` | Video: frames per scan, 1–6 (default 4) |
| `--max-scans N` | Cost guard: stop after N scans (default 50) |
| `--dry-run` | List the scans that would run and how many model calls they make. Sends nothing and needs no API key |
| `--json` | Print full results as JSON on **stdout**. Progress always goes to **stderr**, so you can pipe stdout |
| `--fail-on STATUS` | Exit 1 if any item has that status (see below) |
| `--quiet` | Only errors on stderr |

`--fail-on` statuses (each includes those above it):
- `exception`: a quantity difference or a mismatched attribute;
- `check`: exception, or an attribute that's a near match or wasn't read;
- `not_scanned`: either of the above, or an item no scan has matched yet.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Done |
| `1` | `--fail-on` matched |
| `2` | Setup or usage error (listing unreadable, media missing, no AI provider), or every scan failed |

Single scans that fail are reported on stderr and skipped. Check the cost first with
`inventory-checker check --listing items.xlsx photos/ clip.mp4 --dry-run`.

**Example: nightly job**
```bash
inventory-checker check -l listing.xlsx /data/uploads/today/ -o "/data/results/$(date +%F).xlsx" \
  --fail-on exception -q || notify "Inventory exceptions found"
```

**Example: let another program read the results**
```bash
inventory-checker check -l listing.csv shelf.jpg --json -q | jq '.results.summary'
```

The JSON contains:
- `listing` and `details`;
- `detection`: which provider and model ran;
- `results`:
  - `summary`: counts by status;
  - `items`: per item, the expected and counted quantity, a `checks` entry for each attribute (expected, read, result), the status and the review;
  - `not_in_sample`: objects seen that aren't on the listing;
- `scans`: every scan with its detected objects;
- `errors`: scans that failed.

---

## MCP server: `inventory-checker mcp`

Gives an AI agent tools to run a count. It works on one count at a time, kept in memory while the server runs.

| Tool | What it does |
|---|---|
| `load_listing(path)`, `load_listing_csv(csv_text)`, `load_demo_listing()` | Start a count |
| `set_details(client, location, count_date, counter)` | Details for the exported workbook |
| `check_media(paths, every_seconds, segment_frames, max_scans)` | Scan photos, videos or folders from disk |
| `check_image(data_base64, media_type)` | Scan one image sent inline, e.g. pasted into a chat |
| `get_results(status)` | Per-item results, optionally only `exception`, `check`, … |
| `list_scans()` | Scans with object IDs |
| `set_match(scan_id, object_id, item_id)` | Correct a match (`item_id` null = not on the listing) |
| `remove_scan(scan_id)`, `remove_object(scan_id, object_id)` | Discard a scan or one detected object |
| `review_item(item_id, decision, note)` | `accepted` or `follow_up`, with a note |
| `export_results(path)` | Write `.xlsx`, `.csv` or `.json` |

File paths are limited to `--allowed-dir`, which defaults to the folder the server starts in. Anything
outside it is refused.

**Claude Code**
```bash
claude mcp add inventory-checker --env ANTHROPIC_API_KEY=sk-ant-... -- inventory-checker mcp --allowed-dir ~/counts
```
Then ask, for example: *"Load ~/counts/acme/items.xlsx, check the photos in ~/counts/acme/photos, show me the
exceptions and export results.xlsx."*

**Claude Desktop, and other clients configured with JSON**
```json
{
  "mcpServers": {
    "inventory-checker": {
      "command": "/full/path/to/.venv/bin/inventory-checker",
      "args": ["mcp", "--allowed-dir", "/Users/you/counts"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```
Use the full path to `inventory-checker` inside your virtual environment (`which inventory-checker` prints it).

**Over HTTP** (streamable HTTP, for remote or multi-client set-ups):
```bash
inventory-checker mcp --transport http --port 8765        # http://127.0.0.1:8765/mcp
```
The HTTP transport has **no authentication**, so keep it on `127.0.0.1` or put it behind your own
authenticating proxy.

The model the MCP server calls for detection is the one configured in *its* environment (`ANTHROPIC_API_KEY`
etc.), whichever agent is driving it.

---

## Python API

```python
from inventory_checker import InventoryCheck

check = InventoryCheck.from_file("items_to_be_tested.xlsx")        # or .from_bytes(...), .demo()
check.details.update(client="Acme Ltd", counter="R. Auditor")

check.check_paths(["photos/", "aisle3.mp4"], every_seconds=1.0)   # one model call per photo / video segment
print(check.summary)       # {'verified': 4, 'check': 1, 'exception': 1, 'not_scanned': 2, 'total': 8, ...}

for item in check.results()["items"]:
    if item["status"] == "exception":
        print(item["item_id"], item["counted_qty"], "/", item["expected_qty"],
              [(c["name"], c["result"]) for c in item["checks"]])

check.review("A-1002", "follow_up", "label damaged")
check.set_match("S003", "2", "A-1004")   # correct a match (scan id, object id, item id)
check.export("results.xlsx")             # or .csv / .json
```

- **Async code:** use `await check.acheck_paths(...)`, `await check.acheck_frames(frames)` or
  `await check.acheck_segments(...)`. Calling the plain (sync) methods from inside a running event loop raises an error.
- **Frames already in memory:** pass them as `[(jpeg_bytes, "image/jpeg"), ...]`. One frame is a close-up;
  up to six is a video segment.
- **Your own detector:** pass `InventoryCheck(sample, detector=my_async_fn)`, with the same signature as
  `DETECTOR` (see [extending.md](extending.md)).
- **Progress and errors:** `acheck_segments(..., on_scan=..., on_error=...)` reports each scan as it finishes,
  and failed scans are skipped instead of raised.

### As tools for a Microsoft Agent Framework agent

```python
from agent_framework import Agent
from inventory_checker import InventoryCheck
from inventory_checker.providers import build_chat_client

check = InventoryCheck.from_file("items.xlsx")
client, _, _ = build_chat_client()
auditor = Agent(client, "You help auditors verify inventory counts. Use the tools; cite item IDs.",
                tools=check.as_tools(allowed_dir="counts/"))
print(await auditor.run("Check the photos in counts/acme/ and summarise the exceptions."))
```

`as_tools()` returns four tools: `check_media`, `get_results`, `review_item` and `export_results`. File access
is limited to `allowed_dir`.
