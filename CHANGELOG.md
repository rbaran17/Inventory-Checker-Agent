# Changelog

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
