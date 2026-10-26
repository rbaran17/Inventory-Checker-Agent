"""Find the public HTTPS address of a Cloudflare quick tunnel.

``cloudflared tunnel --url ... --metrics <host:port>`` serves ``/quicktunnel`` on its metrics port,
returning ``{"hostname": "<random>.trycloudflare.com"}``. With ``TUNNEL_METRICS_URL`` set (the
Docker Compose setup does this), the app reads the address from there, so ``PUBLIC_BASE_URL``
does not have to be copied by hand after every tunnel restart.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

log = logging.getLogger("inventory_checker")


def discover_quick_tunnel(metrics_url: str, timeout: float = 2.0) -> str | None:
    """Return ``https://<hostname>`` from cloudflared's metrics server, or None if not available yet."""
    url = metrics_url.rstrip("/") + "/quicktunnel"
    try:
        # The metrics server is a local/sidecar address: never send this through an outbound proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            hostname = (json.loads(resp.read().decode() or "{}").get("hostname") or "").strip()
    except (OSError, ValueError) as exc:
        log.debug("Quick tunnel address not available from %s: %s", url, exc)
        return None
    return f"https://{hostname}" if hostname else None


def wait_for_quick_tunnel(metrics_url: str, total_seconds: float = 60, every: float = 2) -> str | None:
    deadline = time.monotonic() + total_seconds
    while time.monotonic() < deadline:
        found = discover_quick_tunnel(metrics_url)
        if found:
            return found
        time.sleep(every)
    return None
