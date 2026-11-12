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
    sub.add_parser("rotate-key", help="Replace the saved access key (old links, QR codes and sessions stop working)")
    args = p.parse_args(argv)

    if args.cmd == "rotate-key":
        from .config import rotate_access_key

        path = rotate_access_key()
        print(f"New access key saved to {path}. Restart the server; it prints the new laptop link.")
        if os.getenv("APP_ACCESS_KEY"):
            print("Note: APP_ACCESS_KEY is set and takes precedence over the saved key. Change it there too.")
        return

    import uvicorn

    from .api import create_app
    from .config import Settings

    settings = Settings()
    app = create_app(settings)
    ai = f"on ({app.state.ai_label})" if app.state.ai_ready else "OFF - configure an AI provider (see README)"
    local = os.getenv("LAPTOP_URL", f"http://localhost:{args.port}").rstrip("/")
    lines = [f"Open on the laptop:  {local}/?key={settings.access_key}", f"AI detection:        {ai}"]
    if len(settings.access_key) < 16:
        lines.append("WARNING: the access key is short and could be guessed. Use 16+ random characters.")
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
