# Deployment

The phone needs an **HTTPS** address, because browsers only allow camera access on secure pages. Every option
below is a way of giving the phone such an address.

## Docker Compose with a quick tunnel (easiest)

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY, or the Azure OpenAI / OpenAI settings
docker compose up --build
```

- `app` listens on `http://localhost:8000`. The port is published on 127.0.0.1 only, for the laptop.
- `tunnel` runs `cloudflared` and creates a free `https://<random>.trycloudflare.com` address.
- The app reads that address from the tunnel's metrics endpoint (`TUNNEL_METRICS_URL`) and puts it in
  the QR code. You don't copy anything by hand.
- The access key is kept in the `app-data` volume, so restarts keep the same link.
- The two containers share a small private network with fixed addresses (`192.168.253.0/29`), so the app
  knows which address is the tunnel and believes the visitor's address only when the tunnel reports it (see
  [Behind a tunnel or proxy](#behind-a-tunnel-or-proxy)). If `docker compose up` fails with *"Pool overlaps
  with other one on this address space"*, that range is taken on your machine: set `NETWORK_SUBNET`, `APP_IP`
  and `TUNNEL_IP` in `.env` to a free range, for example `10.213.77.0/29`, `10.213.77.2` and `10.213.77.3`.

Open the **"Open on the laptop"** link from `docker compose logs app`, then scan the QR code.

`docker compose up app` runs the app without the tunnel. Bring your own HTTPS address with `PUBLIC_BASE_URL`.

## Prebuilt image

Each push to `main` and each version tag publishes a multi-architecture image (`linux/amd64`, `linux/arm64`):

```bash
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  -e PUBLIC_BASE_URL=https://<your-https-address> \
  -v inventory-checker-data:/data ghcr.io/rbaran17/inventory-checker-agent:latest
```

If the repository is private, the image is private too: `docker login ghcr.io` with a token that has
`read:packages`.

## Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[azure]"
cloudflared tunnel --url http://127.0.0.1:8000           # window 1: prints the https address
export PUBLIC_BASE_URL=https://<that-address>             # window 2
inventory-checker serve
```

Use `127.0.0.1`, not `localhost`, in the tunnel command: on macOS `localhost` resolves to IPv6 first, while
the app listens on IPv4.

## Other HTTPS options

| Option | When |
|---|---|
| Cloudflare **named** tunnel | Stable hostname on your own domain, with Cloudflare Access sign-in in front |
| ngrok, Tailscale Funnel, etc. | Any HTTPS tunnel works: set `PUBLIC_BASE_URL` to its address |
| Cloud container service (Azure Container Apps, Cloud Run, App Runner, …) | Run the image. The platform provides HTTPS: set `PUBLIC_BASE_URL` to the app's URL. On Azure, a managed identity plus `AZURE_OPENAI_*` gives keyless model access |
| Local certificate (`mkcert`) | Same Wi-Fi only. Run `serve --certfile … --keyfile …` and trust the CA on the phone |

The app keeps state in memory, so run **one** replica.

## Behind a tunnel or proxy

Wrong access keys are counted per visitor address, and ten of them block that address for five minutes. With
a tunnel or proxy in front, every visitor arrives from the proxy's address, and the real one is in the
`X-Forwarded-For` header the proxy adds. The app believes that header only when the connection comes from an
address listed in `FORWARDED_ALLOW_IPS`. Any other sender's header is ignored, because whoever can write it
can pick the address they are counted under: a new one for every guess, or yours, to lock you out.

| Setup | What to set |
|---|---|
| Tunnel or proxy on the same machine (`cloudflared tunnel --url http://127.0.0.1:8000`, ngrok, …) | Nothing. The default trusts this machine only (`127.0.0.1,::1`) |
| Docker Compose from this repository | Nothing. `compose.yaml` sets it to the tunnel container's fixed address |
| The app in Docker (`docker compose up app`), your own tunnel or proxy on the host | The address the app sees the host as: the Docker network's gateway. With this repository's `compose.yaml`, `FORWARDED_ALLOW_IPS=192.168.253.1` in `.env` |
| Proxy or load balancer on another machine | That machine's address, or several separated by commas. Recent Uvicorn versions also accept a network such as `10.0.0.0/24`; 0.30, the oldest this project supports, does not |

If the proxy is not listed, the app still works, but all visitors share the proxy's address: ten wrong keys
from anyone then block sign-in, and pages that are already signed in, for everyone behind it for up to five
minutes at a time. Never set `*`. Only list a proxy that adds `X-Forwarded-For` itself, as Cloudflare and
ngrok do.

## Building behind a corporate proxy

If your network inspects TLS, give the build your proxy's CA bundle as a build secret:

```bash
docker build --secret id=pip_ca,src=/path/to/corporate-ca.crt \
  --build-arg HTTPS_PROXY=$HTTPS_PROXY -t inventory-checker-agent:local .
```

The CA is only used for `pip` during the build and is not stored in the image.

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `AI_PROVIDER` | auto | `anthropic`, `azure_openai`, `openai` or `none` (see [providers.md](providers.md)) |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_EFFORT` | –, `claude-haiku-5-5`, `low` | Anthropic |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` | –, –, Entra ID, `2024-10-21` | Azure OpenAI |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | – | OpenAI |
| `AI_MAX_TOKENS` | `4000` | Output cap per scan |
| `DETECTOR` | – | `module:function` custom detector |
| `PUBLIC_BASE_URL` | discovered / laptop address | HTTPS address the QR code points to |
| `TUNNEL_METRICS_URL` | – | cloudflared metrics address for automatic discovery |
| `APP_ACCESS_KEY` | generated once | Fixed access key. At least 16 characters, or the server refuses to start |
| `APP_ACCESS_KEY_FILE` | `~/.config/inventory-checker/access_key` (`/data/access_key` in Docker) | Where the generated key is saved |
| `SAMPLE_FILE` | – | Items to be tested listing (CSV or .xlsx) loaded into the Engagement tab at startup |
| `HOST`, `PORT` | `127.0.0.1`, `8000` (`0.0.0.0` in Docker) | Listen address |
| `LAPTOP_URL` | `http://localhost:<port>` | Base of the printed laptop link |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Addresses whose `X-Forwarded-For` is believed (see [Behind a tunnel or proxy](#behind-a-tunnel-or-proxy)) |
| `NETWORK_SUBNET`, `APP_IP`, `TUNNEL_IP` | `192.168.253.0/29`, `.2`, `.3` | Docker Compose only: the private network between the app and the tunnel |
| `MAX_UPLOAD_BYTES` | 15 MB | Upload limit per scan or listing; larger requests are refused unread |
| `MAX_SCANS_PER_MINUTE`, `MAX_CONCURRENT_SCANS` | `30`, `2` | Caps on model calls |
| `MAX_FRAME_STORE_BYTES` | 256 MB | Scan images kept in memory for the laptop page, per tab. Past it the oldest scans lose their images; their results stay |

Run `inventory-checker rotate-key` (in Docker: `docker compose run --rm app inventory-checker rotate-key`) to
replace the saved access key, then restart.
