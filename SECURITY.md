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
  - Ten wrong keys from one address block further attempts from it for five minutes.
  - The key is kept out of the server's access log, but the laptop link printed at startup contains it:
    treat the terminal output and `docker compose logs` as secret.
  - To replace the key, run `inventory-checker rotate-key` and restart. Old links, QR codes and sessions
    stop working. Do this if a link or a screenshot of the QR code was shared by mistake.
- **Uploaded listings and labels are untrusted.** Listings are parsed with row, column and size limits, and
  text that a spreadsheet would run as a formula is neutralised in the CSV and Excel exports.
- **Scans cost money.** `MAX_SCANS_PER_MINUTE` and `MAX_CONCURRENT_SCANS` cap model calls for anyone holding
  a key.
- **Quick tunnels are public.** A Cloudflare quick tunnel gives anyone with the address a route to the app;
  the access key is what keeps them out. Use a named tunnel or your own HTTPS ingress for anything beyond a demo.
- **No persistence or audit trail.** Scans live in memory and are lost on restart.
