# Inventory-Checker-Agent

Proof of concept for the auditor's inventory count, built on **Microsoft Agent Framework (MAF)** with
**Claude Haiku 5.5**:

1. **Population.** The client's inventory listing (a fake one of everyday items is built in).
2. **Sample.** Every item above a value threshold, plus a random selection.
3. **Count.** Point the phone camera at the location. While you pan, the phone sends short video
   segments (4 frames), and Claude identifies and counts the objects.
4. **Match.** Each detected object is mapped to a population SKU, and the sampled items are marked
   *Match*, *Difference* or *Not counted yet* against the book quantity.

```
 Phone /scan                          Laptop /
 ┌──────────────────┐  4 frames ┌──────────────────────────────┐
 │ camera preview   │──────────▶│ MAF agent → Claude Haiku 5.5 │  one call per segment
 │ Scan video/Photo │◀──────────│ objects + counts + SKU match │
 └──────────────────┘ detections└──────────────┬───────────────┘
                                               ▼
                          sample vs count table, detections, CSV
```

## Run it

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

The phone needs HTTPS. The simplest way is a Cloudflare quick tunnel:

```bash
# window 1
cloudflared tunnel --url http://127.0.0.1:8000
# window 2 (use the https://….trycloudflare.com address window 1 prints)
export PUBLIC_BASE_URL=https://<random-words>.trycloudflare.com
inventory-checker serve
```

`serve` prints a link that includes an access key. Open it on the laptop, then scan the QR code with
the phone. Without the key, the app refuses every request, so nobody who finds the tunnel address can
use it or your API credits. A new key is generated at each start; set `APP_ACCESS_KEY` to keep one.

## Using it

| Where | What to do |
|---|---|
| Laptop: *Client population* | Fake population of 35 items is loaded. Upload a CSV (`sku, description, location, book_qty, unit_cost`) to replace it |
| Laptop: *Draw the sample* | Random items + value threshold + seed. Redrawing keeps the scans and re-checks them |
| Phone | **Start camera**, then **Scan video** and pan slowly (tap **Stop** to end), or **Photo** for a single frame |
| Laptop: *Detections* | Each scan with its frames and objects. Fix a wrong match with the dropdown (★ = in the sample) |
| Laptop: *Sample vs camera count* | Status per sampled item, plus lists of items seen but not sampled and seen but not in the population. **Download results CSV** |

The count for a SKU is the **highest count in any single scan**, so filming the same shelf twice
doesn't double count. Within a scan, Claude is told the frames overlap and to count each physical item once.

## Cost

Per segment: one Claude Haiku 5.5 request with 4 frames downscaled on the phone to 1280 px, effort
`low` and at most 4,000 output tokens. That is about 6,000 input tokens and a few hundred output
tokens, roughly **$0.001 per segment**, or about a cent per minute of scanning, at
$0.10 / $0.50 per million input / output tokens. The phone waits for each answer before recording
the next segment, so calls never pile up. Pressing Stop mid-segment discards that segment.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Required for detection |
| `CLAUDE_MODEL` | `claude-haiku-5-5` | Model |
| `CLAUDE_EFFORT` | `low` | `low` … `max`; higher is slower and costs more |
| `CLAUDE_MAX_TOKENS` | `4000` | Output cap per segment |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Address the QR code points to |
| `APP_ACCESS_KEY` | random | Fixed access key |

## Code

| File | Purpose |
|---|---|
| `src/inventory_checker/detection.py` | MAF `Agent` on Claude Haiku 5.5 with structured output (`SceneResult`). Invented SKUs are dropped in code |
| `src/inventory_checker/population.py` | Fake population, CSV import, sampling, assessment |
| `src/inventory_checker/api.py` | FastAPI app, access key, in-memory state |
| `src/inventory_checker/static/` | `scan.html` (phone) and `dashboard.html` (laptop) |

Tests: `pip install -e ".[dev]" && pytest`. The detector is tested against a local fake of the
Claude API, so the tests spend no credits.

## Limits of this POC

- State is in memory, so restarting clears scans.
- There is no audit trail, sign-in or evidence hashing. The earlier full observation app is in this
  branch's history at commit `6aec8ce`.
- Counting is per view. A long shelf filmed in several segments is not stitched together: count it
  section by section, or rely on the per-SKU maximum.
- Photos are sent to Anthropic's API. Use the fake population and test objects, not client data,
  until the firm approves the environment.
- `skills/audit-knowledge/` is the placeholder slot from the earlier app. This POC doesn't use it.
