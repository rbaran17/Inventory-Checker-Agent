# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's **Security → Report a vulnerability** on this
repository, not in public issues. Include steps to reproduce and the version or commit you tested.

## Scope and known limits

This is a proof of concept. Before using it with real client data, review at least:

- **Data leaves the device.** Photos are sent to the configured model provider (Anthropic, Azure OpenAI or
  OpenAI). Use a provider and region your organisation has approved; Azure OpenAI in your own tenant keeps
  data inside your Azure boundary.
- **Access control is a shared key.** Every request needs the access key (in the printed laptop link) or
  the phone key derived from it (in the QR code). There is no per-user sign-in. Put the app behind your
  identity provider for real use.
  - The phone key can scan, list its scans and remove scans. It cannot read or change the listing, export
    results or show the QR code.
  - Browsers hold a derived session cookie for 12 hours, never the access key itself.
  - Ten wrong keys or wrong session cookies from one address block that address for five minutes: no key
    and no session is accepted from it until then, including the right ones. An IPv6 address is counted
    with its /64.
  - Behind a tunnel or proxy, the visitor's address is taken from `X-Forwarded-For` only when the proxy is
    listed in `FORWARDED_ALLOW_IPS` (default: this machine only). If your proxy is not listed, all visitors
    share its address and one of them can block the others. See
    [docs/deployment.md](docs/deployment.md#behind-a-tunnel-or-proxy).
  - A key you set yourself (`APP_ACCESS_KEY`) must have at least 16 characters, or the server does not
    start. Use random characters: the phone link and the cookies are derived from the key, so a guessable
    key can be tested offline by anyone who has seen a QR code.
  - The key is kept out of the server's access log, but the laptop link printed at startup contains it:
    treat the terminal output and `docker compose logs` as secret.
  - To replace the key, run `inventory-checker rotate-key` and restart. Old links, QR codes and sessions
    stop working. Do this if a link or a screenshot of the QR code was shared by mistake.
- **Uploaded listings and labels are untrusted.** A listing may have at most 5,000 rows, 60 columns,
  500 characters per cell (longer cells are cut) and 4,000,000 characters of text in total, where each item
  counts its cells and its column names. An Excel file may unpack to at most 64 MB, of which 2 MB for
  cell formats, 8 MB for the shared text table, 2 MB for each of its other housekeeping parts and 16 MB
  for any other single XML part, such as a sheet. Text
  that a spreadsheet would run as a formula is neutralised in the CSV and Excel exports, column names
  included; control characters are removed.
- **Photos and videos given to the headless tools are untrusted too.** A photo is only decoded as JPEG,
  PNG, WebP, BMP, TIFF or GIF and only up to 100 megapixels; a video only as MP4/MOV, Matroska/WebM or AVI,
  read from the file itself, so a playlist renamed to `.mp4` cannot make the decoder fetch a URL or read
  another file. A folder scan skips links that lead out of the folder.
- **Scans cost money.** `MAX_SCANS_PER_MINUTE` and `MAX_CONCURRENT_SCANS` cap model calls for anyone holding
  a key.
- **Quick tunnels are public.** A Cloudflare quick tunnel gives anyone with the address a route to the app;
  the access key is what keeps them out. Use a named tunnel or your own HTTPS ingress for anything beyond a demo.
- **No persistence or audit trail.** Scans live in memory and are lost on restart. Scan images are kept up
  to `MAX_FRAME_STORE_BYTES` (256 MB) per tab; past that the oldest scans keep their results but lose
  their images.
- **Responses are marked `Cache-Control: no-store`** (stored frames: `private, no-cache`), so a CDN or proxy
  in front of the app must not keep copies of results or exports. Do not configure one to ignore that.
