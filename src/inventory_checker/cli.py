"""Command line: ``inventory-checker serve``."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="inventory-checker")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the scan service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--certfile", help="TLS certificate (not needed behind an HTTPS tunnel)")
    s.add_argument("--keyfile")
    args = p.parse_args(argv)

    import uvicorn

    from .api import create_app
    from .config import Settings
    from .detection import model_name

    settings = Settings()
    app = create_app(settings)
    base = settings.public_base_url.rstrip("/")
    ai = f"on ({model_name()})" if app.state.ai_ready else "OFF - set ANTHROPIC_API_KEY and restart"
    print("\n" + "=" * 76)
    print(f"  Open on the laptop:  {base}/?key={settings.access_key}")
    print(f"  AI detection:        {ai}")
    print("  The phone gets its link from the QR code on that page.")
    print("=" * 76 + "\n", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, ssl_certfile=args.certfile, ssl_keyfile=args.keyfile,
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
