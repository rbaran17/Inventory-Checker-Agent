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
| `APP_ACCESS_KEY` | generated once | Fixed access key |
| `APP_ACCESS_KEY_FILE` | `~/.config/inventory-checker/access_key` (`/data/access_key` in Docker) | Where the generated key is saved |
| `POPULATION_CSV` | built-in fake | Population loaded at startup |
| `HOST`, `PORT` | `127.0.0.1`, `8000` (`0.0.0.0` in Docker) | Listen address |
| `LAPTOP_URL` | `http://localhost:<port>` | Base of the printed laptop link |
| `MAX_UPLOAD_BYTES` | 15 MB | Upload limit per scan |
