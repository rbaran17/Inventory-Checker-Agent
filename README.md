# Inventory Checker Agent

**Count and verify inventory with a phone camera.** An AI agent built on
[Microsoft Agent Framework](https://github.com/microsoft/agent-framework) identifies the objects in a short
video or close-up photo, reads serial numbers, model numbers and other label details, and matches everything
automatically against the auditor's **items to be tested listing**.

Runs with **Anthropic Claude** (default: Claude Haiku 5.5), **Azure OpenAI** or **OpenAI**, all through MAF.
You can also plug in your own detector.

[![CI](https://github.com/rbaran17/inventory-checker-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/rbaran17/inventory-checker-agent/actions/workflows/ci.yml)
License: [MIT](LICENSE) · Status: proof of concept

---

## The workflow

The laptop app walks the auditor through five steps:

| Step | What happens |
|---|---|
| **1. Items to be tested** | Upload the listing (CSV or Excel): item ID, description, expected quantity, location, plus any attributes to verify, such as serial number, model number, manufacturer or asset tag. Add the engagement details |
| **2. Connect phone** | Scan the QR code. The phone opens a secure camera page, with no app to install |
| **3. Scan & match** | **Scan** mode pans across items. **Close-up** mode reads a label. Each object is counted, its label details are read, and it is matched to a listed item. A matching serial number overrides a description match |
| **4. Review exceptions** | For every quantity difference, mismatched or unreadable attribute, or near match (a likely one-character misread), see expected vs read side by side and record **Accept** or **Follow up** with a note |
| **5. Summary & export** | KPIs and a results table. Download an **Excel** workbook (summary, results, detections, objects not on the listing) or a CSV |

The **Demo** tab runs the same workflow on twelve everyday home and office items, with data kept separate
from the engagement. Edit the expected brand, model or serial number of something on your desk to see
matches and exceptions.

```
 Phone  (/scan)                          Laptop  (/)
 ┌────────────────────────┐            ┌─────────────────────────────────────────┐
 │ full-screen camera     │  frames    │ FastAPI                                 │
 │ Scan │ Close-up        │───────────▶│  └─ MAF Agent ── Anthropic | Azure      │
 │ latest result · remove │◀───────────│                  OpenAI | OpenAI | own  │
 │ Results sheet          │  objects,  │  listing · reconcile · compare · review │
 └────────────────────────┘  labels    └─────────────────────────────────────────┘
          ▲  HTTPS (tunnel or your domain)
          └── QR code on the laptop page
```

The model is told **which** attributes to read, never the expected values, so it cannot simply echo
them back. The comparison happens in code.

## Quick start (Docker)

You need Docker and an API key for one provider.

```bash
git clone https://github.com/rbaran17/inventory-checker-agent.git
cd inventory-checker-agent
cp .env.example .env          # set ANTHROPIC_API_KEY=... (or the Azure OpenAI / OpenAI settings)
docker compose up --build
```

1. In the logs, find **"Open on the laptop: http://localhost:8000/?key=…"** and open that link on your laptop.
2. Wait for **"Tunnel ready: https://….trycloudflare.com"**. The QR code in step 2 then points the phone there.
3. Try the **Demo** tab first: scan the QR code with the phone, tap **Start camera**, and point it at things on
   your desk.

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

Open the printed **"Open on the laptop"** link, then follow the steps on the page.

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
**Stop** mid-segment discards that segment. A close-up is one larger frame (2048 px, for small label text),
about half the cost of a video segment. Other providers' prices differ.

## Using it

| Where | What to do |
|---|---|
| Laptop: step 1 | Engagement tab: add client, location, count date and your name, then drop the listing file (or **Download template**). Demo tab: edit expected values if you like |
| Phone | **Start camera**. **Scan**: tap the red button and pan slowly, tap again to stop. **Close-up**: fit the label in the frame and tap |
| Phone: result card | Pops up after each scan. **Remove** discards the scan, and **✕** removes one detected object (for example something in the background) |
| Phone: Results | Thumbnail button, bottom left: every scan with its objects, plus delete per scan (tap twice) or per object |
| Laptop: step 3 | Live detections with frames and label values. Correct a match with the dropdown |
| Laptop: step 4 | Accept or flag each exception, with a note |
| Laptop: step 5 | Results and downloads |

**Listing format.** CSV or `.xlsx`, with one row per item:

```csv
item_id,description,expected_qty,location,serial_number,model_number,manufacturer
IT-0001,Laptop computer,1,Office 2.14,C02XK1ZZJG5H,A2337,Apple
IT-0002,Computer monitor,2,Office 2.14,,P2422H,Dell
```

- Common header variations are recognised (`SKU`, `Qty`, `Book qty`, `Bin`, …).
- Every other column becomes an attribute to verify.
- A blank cell means *record what is read*.
- Values are compared ignoring case, spaces and punctuation.

See [docs/extending.md](docs/extending.md#the-items-to-be-tested-listing) for the full rules.

## Configuration

The most used settings are below. The full table is in [docs/deployment.md](docs/deployment.md#configuration-reference).

| Variable | Default | Purpose |
|---|---|---|
| `AI_PROVIDER` | auto | `anthropic`, `azure_openai`, `openai` or `none` |
| `AI_MAX_TOKENS` | `4000` | Output cap per scan |
| `PUBLIC_BASE_URL` | discovered from tunnel | HTTPS address the phone opens |
| `APP_ACCESS_KEY` | generated once, then reused | Access key in the QR code and laptop link |
| `SAMPLE_FILE` | – | Items to be tested listing loaded into the Engagement tab at startup |
| `DETECTOR` | – | Custom detector `module:function` |

## Extending

- **New model provider:** write a function that returns a MAF chat client and pass it to `register_provider(...)`.
- **Your own detector** (local YOLO or ONNX model, internal service, barcode or RFID reader): set
  `DETECTOR=pkg.module:function`. See [`examples/custom_detector.py`](examples/custom_detector.py).
- **Counting rules:** edit the prompt in `detection.INSTRUCTIONS`.
- **Matching and comparison rules:** edit the pure functions in `sample.py`.
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
  sample.py        items to be tested listing: CSV/Excel import, demo items, reconcile, compare, assess
  export.py        results CSV and Excel workbook
  api.py           FastAPI app, access key, in-memory state
  tunnel.py        discovers the Cloudflare quick-tunnel address
  cli.py           `inventory-checker serve`
  static/          scan.html (phone camera), dashboard.html (laptop workflow)
examples/          example items to be tested listing, custom detector
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
