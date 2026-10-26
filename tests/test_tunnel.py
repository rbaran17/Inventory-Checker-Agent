from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from inventory_checker.api import create_app
from inventory_checker.config import Settings
from inventory_checker.tunnel import discover_quick_tunnel


@pytest.fixture
def metrics():
    state = {"hostname": ""}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/quicktunnel":
                self.send_response(404)
                self.end_headers()
                return
            out = json.dumps({"hostname": state["hostname"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_port}"
    yield state
    srv.shutdown()


def test_discovers_quick_tunnel_hostname(metrics):
    assert discover_quick_tunnel(metrics["url"]) is None  # tunnel not registered yet
    metrics["hostname"] = "calm-river-blue.trycloudflare.com"
    assert discover_quick_tunnel(metrics["url"]) == "https://calm-river-blue.trycloudflare.com"
    assert discover_quick_tunnel("http://127.0.0.1:9") is None  # nothing listening


def test_qr_uses_discovered_tunnel_and_caches_it(metrics):
    st = Settings(public_base_url=None, tunnel_metrics_url=metrics["url"], access_key="k")
    c = TestClient(create_app(st, use_env_client=False))
    c.get("/?key=k", follow_redirects=False)
    r = c.get("/api/qr").json()
    assert r["public"] is False and "not HTTPS" in r["warning"]  # falls back to the laptop's address
    metrics["hostname"] = "calm-river-blue.trycloudflare.com"
    r = c.get("/api/qr").json()
    assert r["url"] == "https://calm-river-blue.trycloudflare.com/scan?key=k" and r["warning"] is None
    metrics["hostname"] = ""
    assert c.get("/api/qr").json()["public"] is True  # cached


def test_healthz_needs_no_key():
    c = TestClient(create_app(Settings(access_key="k"), use_env_client=False))
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/api/state").status_code == 401
