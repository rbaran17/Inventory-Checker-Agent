# Extending

The app is small on purpose. These are the supported extension points.

## Add a model provider

Any model with a Microsoft Agent Framework chat client can be added without touching the detector:

```python
# my_providers.py
import os
from inventory_checker.providers import Provider, register_provider


def build():
    from agent_framework_anthropic import AnthropicFoundryClient  # example: Claude on Microsoft Foundry
    return AnthropicFoundryClient(model=os.environ["FOUNDRY_MODEL"], resource=os.environ["FOUNDRY_RESOURCE"],
                                  api_key=os.environ["FOUNDRY_API_KEY"])


register_provider(Provider(
    name="claude_foundry",
    is_configured=lambda: bool(os.getenv("FOUNDRY_RESOURCE")),
    build=build,
    model=lambda: os.getenv("FOUNDRY_MODEL", ""),
))
```

Import the module before the app starts (for example from a small wrapper script that imports it and then
calls `inventory_checker.cli.main()`), and select it with `AI_PROVIDER=claude_foundry`. To contribute a provider
upstream, add it to `providers.py` with a fake-API test in `tests/test_detection.py`. The fake must enforce
that provider's structured-output rules.

The chat client must accept image content (`agent_framework.Content.from_data`) and a JSON-schema
`response_format`.

## Replace the detector

To use your own model (a local YOLO or ONNX model, an internal vision service, a barcode or RFID reader),
set `DETECTOR` to an async function:

```bash
DETECTOR=examples.custom_detector:detect inventory-checker serve
```

```python
from inventory_checker.detection import Attribute, DetectedObject, SceneResult
from inventory_checker.sample import Sample

async def detect(frames: list[tuple[bytes, str]], sample: Sample) -> SceneResult:
    # frames: up to 6 (JPEG bytes, media type) from one scan; sample: the items to be tested listing
    ...
    return SceneResult(objects=[DetectedObject(
        name="cordless drill", count=4, confidence=0.9, matched_item_id="WH-0101",
        attributes=[Attribute(name="Model number", value="DCD771C2")])])
```

The app then reconciles each object with the listing (an attribute value that equals an item's expected value
wins over `matched_item_id`), compares the attributes, and updates the UI. See
[`examples/custom_detector.py`](../examples/custom_detector.py).

## The items to be tested listing

Upload it in step 1 of the Engagement tab, or load it at startup with `SAMPLE_FILE=/path/to/file`. In Docker,
mount the file and point the variable at it. CSV (comma, semicolon or tab separated) and Excel (.xlsx, first
sheet) both work. Example: [`examples/items_to_be_tested.csv`](../examples/items_to_be_tested.csv).

| Column | Recognised headers (case and punctuation ignored) | Required |
|---|---|---|
| Item ID | item id, item, id, sku, item number, part number, asset id, reference | No. Generated as `ROW-n` if missing |
| Description | description, item description, name, item name, product | **Yes** |
| Expected quantity | expected qty, qty, quantity, book qty, count, on hand | No. Defaults to 1 |
| Location | location, bin, warehouse, site, area, zone, shelf | No |
| *anything else* | e.g. serial number, model number, manufacturer, asset tag, lot | Each becomes an attribute |

The model matches on the description, so plain, specific descriptions ("Cordless drill", not "SKU-4711 CDL
18V") work best. A blank attribute cell means *record what is read* rather than *verify*.

**How attributes are compared** (`sample.compare`): uppercase, letters and digits only, and prefixes such as
`S/N:` or `Model No.` are dropped. The results are:
- **Match:** equal after normalising.
- **Near match – check:** codes of six characters or more that differ by one character, a typical misread
  such as O/0 or B/8.
- **Mismatch:** values were read but differ.
- **Not read:** nothing legible was read.
- **Recorded:** the expected value was blank, so the value read is captured.

**Item status:**
- **Verified:** quantity matches and every expected attribute matches.
- **Exception:** quantity differs or an attribute mismatches.
- **Check:** quantity matches, but an attribute is a near match or not read.
- **Not scanned:** no scan has matched the item yet.

The counted quantity is the highest count in any single scan.

## Tune the prompt

`detection.INSTRUCTIONS` holds the counting and reading rules. Typical changes: count by case or pallet
instead of by unit, ignore an area, or read a specific label layout. Keep the answer format (`RESPONSE_SCHEMA`)
provider-neutral: every property required, `additionalProperties: false`, and no numeric limits.

## HTTP API

The laptop and phone pages use a small JSON API. Every request needs the access key, sent as the `ic_key`
cookie or a `?key=` query parameter.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Health check (no key needed) |
| GET | `/api/state` | Active tab, listing, scans, assessment, phone status |
| POST | `/api/workspace` | `{"name": "engagement" \| "demo"}`: switch the active tab (phone scans go there) |
| POST | `/api/engagement/details` | `{"client", "location", "count_date", "counter"}` |
| POST / DELETE | `/api/engagement/sample` | Upload (multipart `file`, CSV or .xlsx) / remove the items to be tested listing |
| GET | `/api/sample-template.csv` | Template listing |
| POST | `/api/demo/reset`, `/api/demo/items/{id}` | Reset the demo / edit a demo item's expected qty or attributes |
| GET | `/api/qr` | QR code (SVG) and link for the phone |
| POST | `/api/ping` | Phone heartbeat |
| POST | `/api/scans` | multipart `frames` (1-6 JPEG/PNG/WebP): detect, read and match |
| GET | `/api/phone/scans` | Scans and summary for the phone's Results sheet |
| GET | `/api/scans/{id}/frames/{i}` | A stored frame |
| POST | `/api/scans/{id}/objects/{i}` | `{"item_id": "..."\|null}` correct a match |
| DELETE | `/api/scans/{id}/objects/{i}` | Remove one detected object |
| DELETE | `/api/scans/{id}`, `/api/scans` | Remove one or all scans in the active tab |
| POST | `/api/items/{id}/review` | `{"decision": "accepted"\|"follow_up"\|null, "note"}` |
| GET | `/api/export.xlsx`, `/api/export.csv` | Results for the working papers |
