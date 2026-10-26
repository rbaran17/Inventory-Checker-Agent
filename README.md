# Inventory Checker Agent

**Count inventory with a phone camera.** An AI agent built on
[Microsoft Agent Framework](https://github.com/microsoft/agent-framework) identifies the objects in short video
clips from a phone and matches them automatically to the items an auditor sampled from the client's
inventory listing.

Runs with **Anthropic Claude** (default: Claude Haiku 5.5), **Azure OpenAI** or **OpenAI**, all through MAF.
You can also plug in your own detector.

[![CI](https://github.com/rbaran17/inventory-checker-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/rbaran17/inventory-checker-agent/actions/workflows/ci.yml)
License: [MIT](LICENSE) · Status: proof of concept

---

## What it does

It follows the auditor's physical inventory count:

| Step | Auditor workflow | In the app |
|---|---|---|
| 1 | Receive the client's inventory **population** (book quantities by SKU and location) | A fake population of 35 everyday items is built in. Upload your own CSV |
| 2 | Draw a **sample** | Every item above a value threshold plus a seeded random selection |
| 3 | **Count** the sampled items at the location | Pan the phone camera over the shelf. Every few seconds a short video segment goes to the model, which identifies and counts the objects |
| 4 | **Compare** counts with book quantities | Each detected object is mapped to a SKU. Sampled items show **Match**, **Difference** or **Not counted yet** |

```
 Phone  (/scan)                         Laptop  (/)
 ┌───────────────────────┐            ┌────────────────────────────────────────┐
 │ live camera           │  4 frames  │ FastAPI                                │
 │ Scan video  │  Photo  │───────────▶│  └─ MAF Agent ── Anthropic | Azure     │
 │ detections + matches  │◀───────────│                  OpenAI | OpenAI |     │
 └───────────────────────┘   result   │                  your DETECTOR         │
         ▲  HTTPS                     │  population · sample · assessment      │
         │  (tunnel or your domain)   └────────────────────────────────────────┘
         └── QR code on the laptop page ──┘
```

On the laptop you see the phone's connection status, every scan with its frames, the detected objects (with a
dropdown to correct any match), and the sample-vs-count table, which you can download as CSV.

## Quick start (Docker)

You need Docker and an API key for one provider.

```bash
git clone https://github.com/rbaran17/inventory-checker-agent.git
cd inventory-checker-agent
cp .env.example .env          # set ANTHROPIC_API_KEY=... (or the Azure OpenAI / OpenAI settings)
docker compose up --build
```

1. In the logs, find **"Open on the laptop: http://localhost:8000/?key=…"** and open that link on your laptop.
2. Wait for **"Tunnel ready: https://….trycloudflare.com"**. The QR code on the page then points the phone there.
3. Scan the QR code with the phone, tap **Start camera**, then **Scan video** (pan slowly) or **Photo**.

Compose runs two containers: `app`, and `tunnel`. `tunnel` is a free Cloudflare quick tunnel that gives the
phone the HTTPS address its browser needs before it allows the camera. The app reads the tunnel's address
by itself.

## Quick start (Python)

Python 3.10 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[azure]"
export ANTHROPIC_API_KEY=sk-ant-...        # or the Azure OpenAI / OpenAI settings below
```

**Window 1, HTTPS tunnel for the phone** (`brew install cloudflared`, `winget install Cloudflare.cloudflared`, or
[download it](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)):

```bash
cloudflared tunnel --url http://127.0.0.1:8000     # copy the https://….trycloudflare.com address
```

**Window 2, the app:**

```bash
export PUBLIC_BASE_URL=https://<random-words>.trycloudflare.com
inventory-checker serve
```

Open the printed **"Open on the laptop"** link, then scan the QR code with the phone.

> Use `127.0.0.1`, not `localhost`, in the tunnel command: on macOS `localhost` resolves to IPv6 first, while
> the app listens on IPv4.

## Choosing a model provider

Every provider is a Microsoft Agent Framework chat client. Set one group of variables. If several are
configured, `AI_PROVIDER` chooses; otherwise the first configured provider in this order wins.

| Provider | `AI_PROVIDER` | MAF client | Settings |
|---|---|---|---|
| **Anthropic Claude** | `anthropic` | `agent_framework_anthropic.AnthropicClient` | `ANTHROPIC_API_KEY`; optional `CLAUDE_MODEL` (default `claude-haiku-5-5`), `CLAUDE_EFFORT` (default `low`) |
| **Azure OpenAI** | `azure_openai` | `agent_framework.openai.OpenAIChatCompletionClient` with `azure_endpoint` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, and `AZURE_OPENAI_API_KEY` **or** Entra ID (managed identity / `az login`) |
| **OpenAI** | `openai` | `agent_framework.openai.OpenAIChatCompletionClient` | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| **Your own code** | — | — | `DETECTOR=package.module:function` |

**Anthropic**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Azure OpenAI** (use a vision-capable chat deployment)
```bash
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export AZURE_OPENAI_DEPLOYMENT=<deployment-name>
export AZURE_OPENAI_API_KEY=...          # leave unset to sign in with Entra ID instead
```
For keyless access, give the identity the **Cognitive Services OpenAI User** role on the resource. Azure
OpenAI in your own tenant keeps images inside your Azure boundary, which is often the simplest route to
approval for client data.

**OpenAI**
```bash
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=<vision-capable model>
```

The same agent, prompt and strict JSON answer schema are used with every provider. The schema follows
the structured-output rules of both Anthropic and OpenAI, and the tests check it against fakes of all three
APIs. Details, keyless Azure setup and model choice: **[docs/providers.md](docs/providers.md)**.

## Cost

One request per scan. A 4-frame segment is about 6,000 input tokens plus a few hundred output tokens: roughly
**$0.001** on Claude Haiku 5.5, or about a cent per minute of continuous scanning. Frames are downscaled on
the phone before upload, and the phone waits for each answer before recording the next segment. Pressing
**Stop** mid-segment discards that segment. Other providers' prices differ.

## Using it

| Where | What to do |
|---|---|
| Laptop: **Client population** | Upload a CSV with `sku, description, location, book_qty, unit_cost`, or keep the fake one. Example: [`examples/population.csv`](examples/population.csv) |
| Laptop: **Draw the sample** | Random count, value threshold and seed. Redrawing keeps existing scans and re-checks them |
| Phone | **Start camera**, then **Scan video** to pan (tap **Stop** to end), or **Photo** for one frame |
| Laptop: **Detections** | Each scan with frames and objects. Fix a wrong match in the dropdown (★ = sampled item) |
| Laptop: **Sample vs camera count** | Status per sampled item, plus items seen but not sampled and items not in the population. **Download results CSV** |

The counted quantity for a SKU is the **highest count in any single scan**, so filming a shelf twice doesn't
double count. Within a scan the model is told the frames overlap and to count each item once.

## Configuration

The most used settings are below. The full table is in [docs/deployment.md](docs/deployment.md#configuration-reference).

| Variable | Default | Purpose |
|---|---|---|
| `AI_PROVIDER` | auto | `anthropic`, `azure_openai`, `openai` or `none` |
| `AI_MAX_TOKENS` | `4000` | Output cap per scan |
| `PUBLIC_BASE_URL` | discovered from tunnel | HTTPS address the phone opens |
| `APP_ACCESS_KEY` | generated once, then reused | Access key in the QR code and laptop link |
| `POPULATION_CSV` | built-in fake | Population loaded at startup |
| `DETECTOR` | – | Custom detector `module:function` |

## Extending

- **New model provider:** write a function that returns a MAF chat client and pass it to `register_provider(...)`.
- **Your own detector** (local YOLO or ONNX model, internal service, barcode or RFID reader): set
  `DETECTOR=pkg.module:function`. See [`examples/custom_detector.py`](examples/custom_detector.py).
- **Counting rules:** edit the prompt in `detection.INSTRUCTIONS`.
- **Sampling and matching:** edit the pure functions in `population.py`.
- **API:** small JSON API used by both pages.

See **[docs/extending.md](docs/extending.md)**.

## Deployment

The phone needs HTTPS. Options:
- the built-in Compose tunnel;
- any tunnel with `PUBLIC_BASE_URL`;
- a cloud container service such as Azure Container Apps, where a managed identity gives keyless Azure OpenAI access;
- a local certificate for same-Wi-Fi testing.

Prebuilt images are published to `ghcr.io/rbaran17/inventory-checker-agent` (`linux/amd64`, `linux/arm64`).
Building behind a TLS-inspecting corporate proxy is supported with a build secret.
See **[docs/deployment.md](docs/deployment.md)**.

## Project layout

```
src/inventory_checker/
  detection.py     MAF agent, prompt, provider-neutral answer schema, custom detector loader
  providers.py     Anthropic / Azure OpenAI / OpenAI chat clients and the provider registry
  population.py    fake population, CSV import, sampling, assessment
  api.py           FastAPI app, access key, in-memory state
  tunnel.py        discovers the Cloudflare quick-tunnel address
  cli.py           `inventory-checker serve`
  static/          scan.html (phone), dashboard.html (laptop)
examples/          sample population CSV, custom detector
docs/              providers, extending, deployment
tests/             unit and API tests; providers tested against local fakes of their APIs
Dockerfile, compose.yaml, .github/workflows/
```

## Development

```bash
pip install -e ".[dev,azure]"
pytest                          # no API keys or network needed
ruff check src tests examples
docker build -t inventory-checker-agent:local .
```

CI runs lint and tests on Python 3.10–3.13, builds the package and the image, and smoke-tests the container.
Pushes to `main` publish the image to GHCR. Tags `vX.Y.Z` also publish a GitHub release with the wheel and
source archive. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tunnel log: "Unable to reach the origin service" | The app isn't running, or the tunnel points at `localhost` (use `127.0.0.1`) |
| Every request `401 Unauthorized` / red "access key" banner | Open the **"Open on the laptop"** link again and rescan the QR code |
| Camera never asks for permission | The phone page isn't HTTPS. Use the tunnel address, and open it in the phone's browser itself |
| "AI off" badge / **AI detection: OFF** | No provider configured in the environment the server runs in |
| Red **"Scan not saved"** on the phone | The model call failed. The reason is shown there, and the server log has details (key, model access, credits) |
| QR code shows a "not HTTPS" warning | Tunnel not ready yet (it re-checks every 5 s), or set `PUBLIC_BASE_URL` |

## Limits and security

This is a **proof of concept**:
- State is in memory: restarting clears it, so run one instance.
- There is no per-user sign-in or audit trail. Access is a shared key.
- Photos go to the configured model provider. Use synthetic data unless your organisation has approved
  that provider.

See [SECURITY.md](SECURITY.md).

`skills/audit-knowledge/` is a reserved placeholder for a future audit-methodology knowledge skill. The
current app doesn't use it.

## License

[MIT](LICENSE)
