"""Command line.

- ``inventory-checker serve``       the laptop + phone web app
- ``inventory-checker check``       headless: photos/videos on disk -> results file, no browser or phone
- ``inventory-checker mcp``         MCP server for AI agents (Claude Code, Claude Desktop, ...)
- ``inventory-checker rotate-key``  replace the web app's saved access key
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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

    c = sub.add_parser("check", help="Headless run: scan photos/videos against a listing and write results",
                       description="Scan photos and videos against an items to be tested listing, without the web app. "
                                   "One model call per photo and per video segment.")
    src = c.add_mutually_exclusive_group(required=True)
    src.add_argument("--listing", "-l", help="Items to be tested listing (.csv or .xlsx)")
    src.add_argument("--demo", action="store_true", help="Use the demo listing of everyday items")
    c.add_argument("media", nargs="*", help="Photos, videos, or folders of them")
    c.add_argument("--out", "-o", action="append", default=[], help="Results file (.xlsx, .csv or .json); repeatable")
    c.add_argument("--every", type=float, default=1.0, help="Video: seconds between frames (default 1.0)")
    c.add_argument("--segment-frames", type=int, default=4, help="Video: frames per scan (default 4, max 6)")
    c.add_argument("--max-scans", type=int, default=50, help="Stop after this many scans (default 50)")
    c.add_argument("--client", default="")
    c.add_argument("--location", default="")
    c.add_argument("--count-date", default="")
    c.add_argument("--counter", default="", help="Who counted (shown in the workbook)")
    c.add_argument("--json", action="store_true", help="Print the full results as JSON on stdout")
    c.add_argument("--fail-on", choices=["never", "exception", "check", "not_scanned"], default="never",
                   help="Exit with status 1 if any item has this status (check also covers exception; not_scanned "
                        "covers both and unscanned items)")
    c.add_argument("--dry-run", action="store_true", help="List the scans that would run, without calling a model")
    how = c.add_mutually_exclusive_group()
    how.add_argument("--offline", action="store_true",
                     help="Try the whole flow with no API key: a stand-in detector, no model call, results not real")
    how.add_argument("--detector", metavar="MODULE:FUNCTION",
                     help="Your own detector instead of a hosted model (same as the DETECTOR setting)")
    c.add_argument("--quiet", "-q", action="store_true", help="Only errors on stderr")

    m = sub.add_parser("mcp", help="Run as an MCP server for AI agents")
    m.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    m.add_argument("--host", default="127.0.0.1", help="HTTP transport: address to listen on")
    m.add_argument("--port", type=int, default=8765, help="HTTP transport: port")
    m.add_argument("--allowed-dir", default=None, help="Folder the tools may read/write (default: current folder)")
    args = p.parse_args(argv)

    if args.cmd == "check":
        sys.exit(run_check(args))
    if args.cmd == "mcp":
        sys.exit(run_mcp(args))

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


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run_check(args: argparse.Namespace) -> int:
    """Exit codes: 0 done, 1 --fail-on matched, 2 usage/setup error or every scan failed."""
    from .engine import InventoryCheck
    from .media import MediaError, segments

    say = (lambda *_: None) if args.quiet else _err
    if not 1 <= args.segment_frames <= 6:
        _err("--segment-frames must be between 1 and 6")
        return 2
    from . import detection
    from .sample import demo_sample, parse_sample

    try:
        if args.demo:
            sample = demo_sample()
        else:
            with open(args.listing, "rb") as fh:
                sample = parse_sample(fh.read(), os.path.basename(args.listing))
    except (OSError, ValueError) as exc:
        _err(f"Listing: {exc}")
        return 2
    try:
        if args.dry_run:
            kw = {"detector": _never, "ai_label": "dry run"}
        elif args.offline:
            kw = {"detector": detection.new_offline_detector(), "ai_label": detection.OFFLINE_LABEL}
        elif args.detector:
            kw = {"detector": detection.load_custom_detector(args.detector), "ai_label": f"custom: {args.detector}"}
        else:
            kw = {}
        check = InventoryCheck(sample, **kw)
    except (ValueError, TypeError) as exc:  # a detector that cannot be loaded, or an unknown AI_PROVIDER
        _err(f"Detection: {exc}")
        return 2
    check.details.update(client=args.client, location=args.location, count_date=args.count_date, counter=args.counter)
    say(f"Listing: {check.sample.name} - {len(check.sample.items)} items; attributes: "
        f"{', '.join(check.sample.attribute_names) or 'none'}")
    try:
        segs = list(segments(args.media, args.every, args.segment_frames))
    except MediaError as exc:
        _err(str(exc))
        return 2
    if not segs and not args.dry_run:  # a dry run with nothing to scan still reports 0 model calls
        _err("No photos or videos to scan: give image or video files, or a folder that contains some "
             "(.jpg, .png, .webp, .mp4, .mov, ...)")
        return 2
    planned = segs[:args.max_scans]
    if len(segs) > len(planned):
        say(f"Note: {len(segs)} scans found, only the first {len(planned)} will run (--max-scans)")
    if args.dry_run:
        for i, seg in enumerate(planned, 1):
            say(f"  {i:>3}. {seg.source.name} {seg.label} ({len(seg.frames)} frame{'s' if len(seg.frames) > 1 else ''})")
        say(f"Dry run: {len(planned)} model call(s) would be made. Nothing was sent.")
        return 0
    if not check.ai_ready:
        _err("No AI provider is configured: set ANTHROPIC_API_KEY (or the Azure OpenAI / OpenAI settings, see README).\n"
             "To try the flow without a key, add --offline (a stand-in detector; results are not real).")
        return 2
    say(f"Detection: {check.ai_label} - {len(planned)} scan(s)")
    if args.offline:
        _err("NOTE: --offline does not look at the images. Use it to try the flow, never for a real count.")
    errors: list[str] = []

    def on_scan(seg, scan) -> None:
        objs = ", ".join(f"{o['name']} x{o['count']}" + (f" -> {o['item_id']}" if o.get("item_id") else "")
                         for o in scan["objects"]) or "nothing identified"
        say(f"  {scan['id']} {seg.source.name} {seg.label}: {objs}")

    def on_error(seg, exc) -> None:
        errors.append(f"{seg.source.name} {seg.label}: {exc}")
        _err(f"  FAILED {seg.source.name} {seg.label}: {exc}")

    import asyncio

    asyncio.run(check.acheck_segments(planned, on_scan=on_scan, on_error=on_error))
    for out in args.out:
        try:
            say(f"Wrote {check.export(out)}")
        except (OSError, ValueError) as exc:
            _err(f"Could not write {out}: {exc}")
            return 2
    res = check.results()
    s = res["summary"]
    say(f"Result: {s['verified']} verified, {s['check']} check, {s['exception']} exception, "
        f"{s['not_scanned']} not scanned (of {s['total']}); {len(res['not_in_sample'])} object(s) not on the listing")
    if args.json:
        print(json.dumps({**check.to_dict(), "errors": errors}, indent=2, default=str))
    if planned and len(errors) == len(planned):
        return 2
    fail = {"never": 0, "exception": s["exception"], "check": s["exception"] + s["check"],
            "not_scanned": s["exception"] + s["check"] + s["not_scanned"]}[args.fail_on]
    return 1 if fail else 0


async def _never(frames, sample):  # dry run: never called
    raise RuntimeError("dry run")


def run_mcp(args: argparse.Namespace) -> int:
    try:
        from .mcp_server import build_server
    except ImportError:
        _err("The MCP server needs the 'mcp' extra: pip install 'inventory-checker-agent[mcp]'")
        return 2
    server = build_server(allowed_dir=args.allowed_dir)
    if args.transport == "stdio":
        server.run("stdio")  # stdout carries the protocol: log to stderr only
    else:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            _err("WARNING: the MCP HTTP transport has no authentication. Only expose it behind your own auth.")
        _err(f"MCP server on http://{args.host}:{args.port}/mcp (files limited to {args.allowed_dir or os.getcwd()})")
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    main()
