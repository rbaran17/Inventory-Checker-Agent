# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's **Security → Report a vulnerability** on this
repository, not in public issues. Include steps to reproduce and the version or commit you tested.

## Scope and known limits

This is a proof of concept. Before using it with real client data, review at least:

- **Data leaves the device.** Photos are sent to the configured model provider (Anthropic, Azure OpenAI or
  OpenAI). Use a provider and region your organisation has approved; Azure OpenAI in your own tenant keeps
  data inside your Azure boundary.
- **Access control is a shared key.** Every request needs the access key (in the QR code and the printed
  laptop link). There is no per-user sign-in. Put the app behind your identity provider for real use.
- **Quick tunnels are public.** A Cloudflare quick tunnel gives anyone with the address a route to the app;
  the access key is what keeps them out. Use a named tunnel or your own HTTPS ingress for anything beyond a demo.
- **No persistence or audit trail.** Scans live in memory and are lost on restart.
