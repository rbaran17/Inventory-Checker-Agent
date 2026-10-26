# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,azure]"
pytest            # no API keys needed: providers are tested against local fakes of their APIs
ruff check src tests examples
```

Run the app locally with `inventory-checker serve` (see the README for the phone/HTTPS setup).

## Guidelines

- Keep the app runnable with **no AI key at all** (the UI must still load and explain what is missing).
- Provider-specific code belongs in `src/inventory_checker/providers.py`; the detector in `detection.py`
  must stay provider-neutral (one strict JSON schema that every supported provider accepts).
- New behaviour needs a test. Tests must not call real model APIs or spend credits.
- Never commit API keys, `.env`, certificates or real client data. Use the fake population for examples.
- Keep pull requests focused, and describe how you tested them.

## Adding a model provider

See [docs/extending.md](docs/extending.md#add-a-model-provider). In short: write a builder that returns a
Microsoft Agent Framework chat client, call `register_provider(...)`, and add a fake-API test like the ones
in `tests/test_detection.py`.

## Releases

Maintainers tag `vX.Y.Z` on `main`. The release workflow publishes the container image to GHCR and attaches
the wheel and source archive to a GitHub release.
