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
from inventory_checker.detection import DetectedObject, SceneResult
from inventory_checker.population import Item

async def detect(frames: list[tuple[bytes, str]], population: list[Item]) -> SceneResult:
    # frames: up to 6 (JPEG bytes, media type) from one scan; population: the client listing
    ...
    return SceneResult(objects=[DetectedObject(name="pallet", count=3, confidence=0.9, matched_sku="WH-0002")])
```

The app still checks `matched_sku` against the population, updates the sample table and the UI. See
[`examples/custom_detector.py`](../examples/custom_detector.py).

## Use your own population

- **At runtime:** upload a CSV on the laptop page (*Client population*).
- **At startup:** set `POPULATION_CSV=/path/to/population.csv`. In Docker, mount the file and point the
  variable at it.

Columns: `sku, description, book_qty` (required) and `location, unit_cost` (optional). See
[`examples/population.csv`](../examples/population.csv). The model matches on `description`, so plain,
specific descriptions ("Cordless drill", not "SKU-4711 CDL 18V") give the best results.

## Tune the prompt

`detection.INSTRUCTIONS` holds the counting rules. Typical changes: count by case or pallet instead of
by unit, ignore an area, or require a readable label before matching. Keep the answer format
(`RESPONSE_SCHEMA`) provider-neutral: every property required, `additionalProperties: false`, and no
numeric limits.

## Change sampling or matching rules

`population.py` holds `draw_sample` (value threshold plus seeded random selection) and `assess` (per-SKU
count = the highest count in any single scan). Both are pure functions with unit tests in
`tests/test_population.py`.

## HTTP API

The laptop and phone pages use a small JSON API. Every request needs the access key, sent as the `ic_key`
cookie or a `?key=` query parameter.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Health check (no key needed) |
| GET | `/api/state` | Population, sample, scans, assessment, phone status |
| GET | `/api/qr` | QR code (SVG) and link for the phone |
| POST | `/api/ping` | Phone heartbeat |
| POST | `/api/population` | `{"csv": "..."}` replace the population (redraws the sample) |
| POST | `/api/population/reset` | Restore the fake population |
| POST | `/api/sample` | `{"size", "value_threshold", "seed"}` draw a sample |
| POST | `/api/scans` | multipart `frames` (1-6 JPEG/PNG/WebP): detect and store a scan |
| GET | `/api/scans/{id}/frames/{i}` | A stored frame |
| POST | `/api/scans/{id}/objects/{i}` | `{"sku": "..."|null}` correct a match |
| DELETE | `/api/scans/{id}`, `/api/scans` | Remove one or all scans |
