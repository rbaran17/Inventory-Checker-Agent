# Changelog

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
