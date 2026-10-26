"""Command line: ``inventory-checker serve``."""

from __future__ import annotations

import argparse
import os
import threading


def _banner(lines: list[str]) -> None:
    print("\n" + "=" * 78)
    for line in lines:
        print(f"  {line}")
    print("=" * 78 + "\n", flush=True)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="inventory-checker")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the scan service")
    s.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    s.add_argument("--certfile", help="TLS certificate (not needed behind an HTTPS tunnel)")
    s.add_argument("--keyfile")
    args = p.parse_args(argv)

    import uvicorn

    from .api import create_app
    from .config import Settings

    settings = Settings()
    app = create_app(settings)
    ai = f"on ({app.state.ai_label})" if app.state.ai_ready else "OFF - configure an AI provider (see README)"
    local = os.getenv("LAPTOP_URL", f"http://localhost:{args.port}").rstrip("/")
    lines = [f"Open on the laptop:  {local}/?key={settings.access_key}", f"AI detection:        {ai}"]
    if settings.public_base_url:
        lines += [f"Public address:      {settings.public_base_url}",
                  "The phone gets its link from the QR code on the laptop page."]
        _banner(lines)
    elif settings.tunnel_metrics_url:
        _banner([*lines, "Public address:      waiting for the tunnel… (printed below when ready)"])

        def announce() -> None:
            from .tunnel import wait_for_quick_tunnel

            found = wait_for_quick_tunnel(settings.tunnel_metrics_url)
            if found:
                settings.public_base_url = found
                _banner([f"Tunnel ready:        {found}", "Reload the laptop page; the QR code now points here."])
            else:
                _banner(["Tunnel address not found after 60 s: check the tunnel container's logs."])

        threading.Thread(target=announce, daemon=True).start()
    else:
        _banner([*lines, "Public address:      not set - the phone needs HTTPS (see README: tunnel)"])
    uvicorn.run(app, host=args.host, port=args.port, ssl_certfile=args.certfile, ssl_keyfile=args.keyfile,
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
