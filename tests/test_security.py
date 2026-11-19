"""Access control, limits, export safety and defensive listing parsing."""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from inventory_checker import export, sample, security
from inventory_checker.api import KEY_ATTEMPT_WINDOW, KEY_ATTEMPTS, create_app
from inventory_checker.config import (
    MIN_ACCESS_KEY_CHARS,
    Settings,
    SetupError,
    load_or_create_access_key,
    rotate_access_key,
)
from inventory_checker.detection import Attribute, DetectedObject, SceneResult

KEY = "test-key-123"


async def mug_detector(imgs, smp):
    return SceneResult(objects=[DetectedObject(name="mug", count=1, confidence=0.9, matched_item_id="DEMO-07"),
                                DetectedObject(name="duck", count=1, confidence=0.5),
                                DetectedObject(name="pen", count=1, confidence=0.5)])


def app_client(detector=mug_detector, **settings):
    st = Settings(public_base_url="https://tunnel.example", access_key=KEY, **settings)
    return TestClient(create_app(st, detector=detector, use_env_client=False))


def laptop(detector=mug_detector, **settings):
    c = app_client(detector, **settings)
    assert c.get(f"/?key={KEY}", follow_redirects=False).status_code == 307
    return c


def phone_for(c: TestClient) -> TestClient:
    """A second browser on the same app that opened the QR link."""
    url = c.get("/api/qr").json()["url"]
    p = TestClient(c.app)
    r = p.get(f"/scan?key={parse_qs(urlparse(url).query)['key'][0]}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/scan"
    return p


def frames(n=1):
    return [("frames", (f"f{i}.jpg", b"\xff\xd8frame", "image/jpeg")) for i in range(n)]


# ----------------------------------------------------------------------------- access key


def test_cookie_never_holds_the_access_key():
    c = app_client()
    r = c.get(f"/?key={KEY}", follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{security.LAPTOP_COOKIE}=") and KEY not in cookie and "HttpOnly" in cookie
    assert c.get("/api/state").status_code == 200


def test_non_ascii_and_wrong_keys_are_refused_not_crashed():
    c = app_client()
    assert c.get("/?key=é").status_code == 401
    assert c.get("/api/state?key=%F0%9F%94%91").status_code == 401
    assert not security.same("é", KEY) and security.same("é", "é")


def test_wrong_keys_are_throttled_per_address():
    c = app_client()
    for _ in range(KEY_ATTEMPTS):
        assert c.get("/?key=guess").status_code == 401
    r = c.get(f"/?key={KEY}", follow_redirects=False)
    assert r.status_code == 429 and "retry-after" in r.headers  # even the right key waits out the window
    # A browser at another address is not affected by that guessing, signed in or not.
    elsewhere = TestClient(c.app, client=("198.51.100.7", 50000))
    assert elsewhere.get(f"/?key={KEY}", follow_redirects=False).status_code == 307
    assert elsewhere.get("/api/state").status_code == 200


def at(app, address: str, **kw) -> TestClient:
    """A browser reaching the app from one client address."""
    return TestClient(app, client=(address, 50000), **kw)


def test_wrong_session_cookies_count_as_wrong_keys(monkeypatch):
    """The session cookie is derived from the key, so guessing cookies is guessing keys: it is limited too."""
    clock = [1000.0]
    monkeypatch.setattr(security, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    app = app_client().app
    signed_in = at(app, "203.0.113.5")
    assert signed_in.get(f"/?key={KEY}", follow_redirects=False).status_code == 307

    guesser = at(app, "203.0.113.5")  # same address, e.g. behind the same router
    for i in range(KEY_ATTEMPTS):
        name = security.LAPTOP_COOKIE if i % 2 else security.PHONE_COOKIE
        guess = security.derive(f"word{i}", "laptop-session" if i % 2 else "phone-session")
        assert guesser.get("/api/state", headers={"Cookie": f"{name}={guess}"}).status_code == 401
    # Over the limit: no more answers for that address, not even for the right cookie or the right key...
    right = {"Cookie": f"{security.LAPTOP_COOKIE}={security.derive(KEY, 'laptop-session')}"}
    r = guesser.get("/api/state", headers=right)
    assert r.status_code == 429 and r.headers["retry-after"] == str(KEY_ATTEMPT_WINDOW)
    assert guesser.get(f"/api/state?key={KEY}").status_code == 429
    assert signed_in.get("/api/state").status_code == 429
    # ...while other addresses carry on, and a request that presents nothing is still just unauthorised.
    assert at(app, "203.0.113.6").get(f"/api/state?key={KEY}").status_code == 200
    assert guesser.get("/api/state").status_code == 401
    # The block does not sign anyone out: after the window the same session works again.
    clock[0] += KEY_ATTEMPT_WINDOW + 1
    assert signed_in.get("/api/state").status_code == 200


def test_a_stale_cookie_is_dropped_so_an_open_tab_does_not_lock_its_address_out():
    before = create_app(Settings(access_key="the-key-before-rotate-key"), detector=mug_detector, use_env_client=False)
    tab = at(before, "203.0.113.5")
    assert tab.get("/?key=the-key-before-rotate-key", follow_redirects=False).status_code == 307
    # The key is rotated and the server restarted; the tab is still open and polls every two seconds.
    tab = at(app_client().app, "203.0.113.5", cookies=tab.cookies)
    for _ in range(KEY_ATTEMPTS + 5):
        assert tab.get("/api/state").status_code == 401
    assert tab.get(f"/?key={KEY}", follow_redirects=False).status_code == 307  # only the first one counted


def test_limiter_bucket_is_a_checked_address_never_the_raw_text():
    key = security.caller_key
    assert key("203.0.113.9") == "203.0.113.9" and key(" 203.0.113.9 ") == "203.0.113.9"
    assert key("::ffff:203.0.113.9") == "203.0.113.9"  # the same client over an IPv4-mapped address
    # One subscriber holds a whole IPv6 /64: a fresh address per guess must not be a fresh allowance.
    assert key("2001:db8:1:2:aaaa::1") == key("2001:db8:1:2:bbbb::2") == "2001:db8:1:2::/64"
    assert key("2001:db8:1:3::1") != key("2001:db8:1:2::1") and key("fe80::1%eth0") == "fe80::/64"
    for junk in (None, "", "testclient", "10.0.0.1, 10.0.0.2", "999.1.1.1", "A" * 60_000, "1.2.3.4" + "0" * 100):
        assert key(junk) == security.UNKNOWN_CALLER, junk
    assert all(len(key(f"{n}{'A' * 60_000}")) < 50 for n in range(3))  # nothing caller-sized is ever kept


def serve_app(monkeypatch, tmp_path, **env):
    """What `inventory-checker serve` hands to uvicorn: (app, keyword arguments, access key)."""
    import uvicorn

    from inventory_checker import cli

    for name in ("APP_ACCESS_KEY", "FORWARDED_ALLOW_IPS", "PUBLIC_BASE_URL", "TUNNEL_METRICS_URL", "SAMPLE_FILE",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "DETECTOR", "AI_PROVIDER",
                 "HOST", "PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ACCESS_KEY_FILE", str(tmp_path / "access_key"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, kw=kw))
    cli.main(["serve"])
    return started["app"], started["kw"], started["app"].state.settings.access_key


def behind(app, trusted: str, address: str) -> httpx.AsyncClient:
    """A client at `address` talking to the app the way uvicorn runs it with --forwarded-allow-ips."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped, client=(address, 4444)), base_url="http://t")


def test_a_client_cannot_choose_the_address_it_is_throttled_under(monkeypatch, tmp_path, capsys):
    app, kw, key = serve_app(monkeypatch, tmp_path)
    assert kw["proxy_headers"] is True and kw["forwarded_allow_ips"] == "127.0.0.1,::1"

    async def run():
        # Straight from the internet: X-Forwarded-For is ignored, so a new value per guess buys nothing...
        async with behind(app, kw["forwarded_allow_ips"], "203.0.113.9") as attacker:
            codes = [(await attacker.get(f"/api/state?key=guess{i}",
                                         headers={"X-Forwarded-For": f"10.0.{i}.1, 203.0.113.9"})).status_code
                     for i in range(KEY_ATTEMPTS + 3)]
            # ...and naming somebody else's address does not lock that person out.
            for _ in range(KEY_ATTEMPTS):
                await attacker.get("/api/state?key=x", headers={"X-Forwarded-For": "172.18.0.1"})
        async with behind(app, kw["forwarded_allow_ips"], "172.18.0.1") as victim:
            return codes, (await victim.get(f"/?key={key}")).status_code

    codes, victim = asyncio.run(run())
    assert codes == [401] * KEY_ATTEMPTS + [429] * 3 and victim == 307


def test_a_trusted_proxy_gives_each_client_its_own_limit(monkeypatch, tmp_path, capsys):
    app, kw, key = serve_app(monkeypatch, tmp_path, FORWARDED_ALLOW_IPS="192.168.253.3")
    assert kw["forwarded_allow_ips"] == "192.168.253.3"

    async def run():
        # The tunnel appends the address it saw; whatever the client wrote in front of it is not believed.
        async with behind(app, "192.168.253.3", "192.168.253.3") as tunnel:
            guessing = [(await tunnel.get(f"/api/state?key=guess{i}",
                                          headers={"X-Forwarded-For": f"10.9.{i}.9, 198.51.100.7"})).status_code
                        for i in range(KEY_ATTEMPTS + 1)]
            # Text that is not an address shares one small bucket instead of becoming a limiter key each.
            junk = [(await tunnel.get("/api/state?key=x",
                                      headers={"X-Forwarded-For": f"{i}{'A' * 60_000}"})).status_code
                    for i in range(KEY_ATTEMPTS + 1)]
            other = (await tunnel.get(f"/?key={key}", headers={"X-Forwarded-For": "198.51.100.8"})).status_code
        return guessing, other, junk

    guessing, other, junk = asyncio.run(run())
    assert guessing == [401] * KEY_ATTEMPTS + [429] and other == 307
    assert junk == [401] * KEY_ATTEMPTS + [429]


def test_serve_warns_when_every_forwarded_header_is_trusted(monkeypatch, tmp_path, capsys):
    serve_app(monkeypatch, tmp_path, FORWARDED_ALLOW_IPS="*")
    assert "FORWARDED_ALLOW_IPS=*" in capsys.readouterr().out


def test_a_short_operator_key_is_refused_at_startup(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as done:
        serve_app(monkeypatch, tmp_path, APP_ACCESS_KEY="correct-horse")
    err = capsys.readouterr().err
    assert done.value.code == 2 and "Traceback" not in err
    assert "APP_ACCESS_KEY is too short (13 characters)" in err and "rotate-key" in err and "token_urlsafe" in err
    with pytest.raises(SetupError, match="at least 16"):
        Settings()
    # Long enough is accepted as given, and the key the app generates itself is unchanged.
    monkeypatch.setenv("APP_ACCESS_KEY", "x" * MIN_ACCESS_KEY_CHARS)
    assert Settings().access_key == "x" * MIN_ACCESS_KEY_CHARS
    _, _, generated = serve_app(monkeypatch, tmp_path)
    assert len(generated) == 24 and "WARNING" not in capsys.readouterr().out


def test_a_short_saved_key_still_starts_but_says_how_to_replace_it(monkeypatch, tmp_path, capsys):
    (tmp_path / "access_key").write_text("abc123def456")  # 12 characters: what versions before 0.5 generated
    _, _, key = serve_app(monkeypatch, tmp_path)
    assert key == "abc123def456" and "rotate-key" in capsys.readouterr().out


# ----------------------------------------------------------------------------- the path the router serves


def asgi_get(app, path: str, raw_path: str | None = None, host: bytes = b"laptop.example") -> int:
    """One GET put straight into the app, as a server would build it: no client library tidies the path or the
    Host header on the way."""
    sent: list[dict] = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": (raw_path or path).encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", host)], "client": ("203.0.113.9", 50000), "server": ("127.0.0.1", 8000)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_host_header_cannot_turn_a_request_into_the_health_check():
    app = app_client().app
    for host in (b"a/healthz?", b"a/healthz#", b"a/healthz"):
        for path in ("/api/state", "/api/export.csv", "/api/qr", "/"):
            assert asgi_get(app, path, host=host) == 401, (host, path)
    assert asgi_get(app, "/healthz") == 200


def test_sign_in_redirect_stays_on_this_site():
    """The redirect that drops the key from the address bar reuses the path, and "//evil.example/" is a path to
    the server but another website to a browser."""
    sent: list[dict] = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "//evil.example/", "raw_path": b"//evil.example/", "query_string": f"key={KEY}".encode(),
             "root_path": "", "headers": [(b"host", b"laptop.example")], "client": ("203.0.113.9", 50000),
             "server": ("127.0.0.1", 8000)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app_client().app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    location = dict(start["headers"])[b"location"].decode()
    assert start["status"] == 307
    assert location == "/evil.example/"


def test_only_the_health_route_itself_skips_the_key():
    app = app_client().app
    # What a server passes on for /healthz%3Fx, /healthz%20 and /healthz%23x: no route serves these, and the
    # answer must be "no key" (401), not the "not found" (404) of a request that got past the key check.
    for path, raw in (("/healthz?x", "/healthz%3Fx"), ("/healthz ", "/healthz%20"), ("/healthz#x", "/healthz%23x"),
                      ("/healthz/", "/healthz/"), ("//healthz", "//healthz"), ("/healthz/../api/state", None)):
        assert asgi_get(app, path, raw) == 401, path
    c = app_client()
    assert c.get("/healthz%3Fx").status_code == 401 and c.get("/healthz%20").status_code == 401


def test_key_check_never_reads_the_path_from_the_rebuilt_url(monkeypatch):
    """Older Starlette versions rebuild request.url from the Host header, so its path can say /healthz for any
    request. Simulate that on whichever version is installed: the key check must not notice."""
    from starlette.datastructures import URL

    monkeypatch.setattr(URL, "path", property(lambda self: "/healthz"))
    c = app_client()
    assert c.get("/api/state").status_code == 401 and c.get("/api/export.csv").status_code == 401
    assert c.get("/").status_code == 401 and c.get("/healthz").status_code == 200
    assert c.get("/api/state").headers["content-type"].startswith("application/json")  # still told apart as API
    p = phone_for(laptop())  # the phone's limits are checked against the real path too
    monkeypatch.setattr(URL, "path", property(lambda self: "/api/ping"))
    assert p.post("/api/workspace", json={"name": "demo"}).status_code == 403


# ----------------------------------------------------------------------------- response headers


def test_responses_are_not_cacheable_by_anything_in_between():
    c = laptop()
    c.post("/api/workspace", json={"name": "demo"})
    c.post("/api/scans", files=frames())
    for path in ("/api/state", "/api/export.csv", "/api/export.xlsx", "/api/sample-template.csv", "/api/qr", "/",
                 "/scan", "/healthz", "/api/nope"):
        r = c.get(path)
        assert r.headers["cache-control"] == "no-store", path
        assert r.headers["x-content-type-options"] == "nosniff", path
    # Refusals and the sign-in redirect (it sets the session cookie) are covered too.
    for r in (app_client().get("/api/export.csv"), app_client().get("/"),
              app_client().get(f"/?key={KEY}", follow_redirects=False),
              phone_for(c).get("/api/export.csv")):
        assert r.status_code in (401, 403, 307) and r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-type-options"] == "nosniff"
    # Stored frames keep their own policy: private to the browser, revalidated with the ETag.
    r = c.get("/api/scans/S001/frames/0")
    assert r.headers["cache-control"] == "private, no-cache" and r.headers["x-content-type-options"] == "nosniff"


def test_phone_link_can_scan_but_not_manage():
    c = laptop()
    c.post("/api/workspace", json={"name": "demo"})
    p = phone_for(c)
    assert "Count Camera" in p.get("/scan").text
    assert p.post("/api/ping").status_code == 200
    scan = p.post("/api/scans", files=frames()).json()["scan"]
    assert p.get("/api/phone/scans").status_code == 200
    assert p.get(f"/api/scans/{scan['id']}/frames/0").status_code == 200
    # Laptop-only: the dashboard, the listing, exports, the QR code, clearing everything.
    assert p.get("/").status_code == 403
    for method, path in [("GET", "/api/state"), ("GET", "/api/qr"), ("GET", "/api/export.csv"),
                         ("DELETE", "/api/engagement/sample"), ("DELETE", "/api/scans"),
                         ("POST", "/api/demo/reset"), ("POST", "/api/workspace")]:
        assert p.request(method, path, json={"name": "demo"} if method == "POST" else None).status_code == 403, path
    assert p.delete(f"/api/scans/{scan['id']}").status_code == 200


def test_key_is_redacted_from_access_log():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", f"/scan?key={KEY}&x=1", "1.1", 307), None)
    assert security.RedactKeyFilter().filter(record)
    assert KEY not in record.getMessage() and "key=[redacted]&x=1" in record.getMessage()


def test_key_file_is_created_and_rotated(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_ACCESS_KEY", raising=False)
    monkeypatch.setenv("APP_ACCESS_KEY_FILE", str(tmp_path / "sub" / "key"))
    first = load_or_create_access_key()
    assert rotate_access_key() == tmp_path / "sub" / "key"
    second = load_or_create_access_key()
    assert second != first and len(second) >= 24 and not (tmp_path / "sub" / "key.new").exists()


# ----------------------------------------------------------------------------- limits


def test_oversized_request_is_refused_before_it_is_read():
    c = laptop(max_upload_bytes=1000)
    big = {"file": ("listing.csv", b"x" * (3 * 1024 * 1024), "text/csv")}
    assert c.post("/api/engagement/sample", files=big).status_code == 413
    r = c.post("/api/ping", headers={"Transfer-Encoding": "chunked"})
    assert r.status_code == 411


def test_stored_scan_images_are_capped_and_results_survive():
    """Every frame of every scan used to stay in memory until the server was restarted."""
    one = len(frames()[0][1][1])
    c = laptop(max_frame_store_bytes=one * 5, max_scans_per_minute=100)
    c.post("/api/workspace", json={"name": "demo"})
    for n in (3, 2, 2):  # frames per scan: 3, then 5 (the limit), then 7
        assert c.post("/api/scans", files=frames(n)).status_code == 200
    s = c.get("/api/state").json()
    # The oldest scan lost its images; nothing else about it changed, and the count and exports are complete.
    assert [(x["id"], x.get("images_dropped", False), len(x["objects"])) for x in s["scans"]] == [
        ("S001", True, 3), ("S002", False, 3), ("S003", False, 3)]
    assert s["assessment"]["summary"]["not_scanned"] == 11 and s["scans"][0]["frames"] == 3
    r = c.get("/api/scans/S001/frames/0")
    assert r.status_code == 404 and "no longer kept" in r.json()["detail"]
    assert c.get("/api/scans/S002/frames/1").status_code == 200 and c.get("/api/scans/S003/frames/1").status_code == 200
    wb = load_workbook(io.BytesIO(c.get("/api/export.xlsx").content))
    assert [row[0].value for row in wb["Detections"].iter_rows(min_row=2)].count("S001") == 3
    # A scan larger than the whole allowance still shows its own images until the next scan arrives.
    assert c.post("/api/scans", files=frames(6)).status_code == 200
    assert c.get("/api/scans/S004/frames/5").status_code == 200 and c.get("/api/scans/S003/frames/0").status_code == 404
    # Each tab has its own allowance.
    c.post("/api/engagement/sample", files={"file": ("l.csv", b"item_id,description\nA,Mug\n", "text/csv")})
    assert c.post("/api/scans", files=frames(4)).status_code == 200
    assert c.get("/api/scans/S005/frames/0").status_code == 200 and c.get("/api/scans/S004/frames/0").status_code == 200


def test_scans_per_minute_are_capped():
    c = laptop(max_scans_per_minute=2)
    c.post("/api/workspace", json={"name": "demo"})
    assert c.post("/api/scans", files=frames()).status_code == 200
    assert c.post("/api/scans", files=frames()).status_code == 200
    r = c.post("/api/scans", files=frames())
    assert r.status_code == 429 and "retry-after" in r.headers


def test_concurrent_scans_are_capped():
    import httpx

    release = asyncio.Event()

    async def slow(imgs, smp):
        await release.wait()
        return SceneResult()

    app = create_app(Settings(access_key=KEY, max_concurrent_scans=1), detector=slow, use_env_client=False)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await c.get(f"/?key={KEY}")
            await c.post("/api/workspace", json={"name": "demo"})
            first = asyncio.create_task(c.post("/api/scans", files=frames()))
            await asyncio.sleep(0.2)
            second = await c.post("/api/scans", files=frames())
            release.set()
            return (await first).status_code, second.status_code

    assert asyncio.run(run()) == (200, 429)


# ----------------------------------------------------------------------------- scan and object identity


def test_scan_ids_are_unique_across_tabs_and_never_reused():
    c = laptop()
    c.post("/api/workspace", json={"name": "demo"})
    assert c.post("/api/scans", files=frames()).json()["scan"]["id"] == "S001"
    c.post("/api/engagement/sample", files={"file": ("l.csv", b"item_id,description\nA,Mug\n", "text/csv")})
    assert c.post("/api/scans", files=frames()).json()["scan"]["id"] == "S002"  # engagement tab, not S001 again
    c.post("/api/engagement/sample", files={"file": ("l.csv", b"item_id,description\nA,Mug\n", "text/csv")})
    assert c.post("/api/scans", files=frames()).json()["scan"]["id"] == "S003"  # replaced listing: no reuse

    # The laptop is on the engagement tab; the phone still shows the demo scan and removes it.
    assert c.get("/api/state").json()["active"] == "engagement"
    assert c.delete("/api/scans/S001").status_code == 200
    s = c.get("/api/state").json()
    assert [x["id"] for x in s["scans"]] == ["S003"] and s["workspaces"]["demo"]["scans"] == 0


def test_objects_keep_their_id_when_another_is_removed():
    c = laptop()
    c.post("/api/workspace", json={"name": "demo"})
    scan = c.post("/api/scans", files=frames()).json()["scan"]
    assert [o["id"] for o in scan["objects"]] == ["1", "2", "3"]
    c.delete("/api/scans/S001/objects/2")                                   # the duck goes
    assert c.delete("/api/scans/S001/objects/2").status_code == 404         # a repeated tap removes nothing else
    s = c.post("/api/scans/S001/objects/3", json={"item_id": "DEMO-10"}).json()  # still the pen
    assert [(o["id"], o["name"], o["item_id"]) for o in s["scans"][0]["objects"]] == [
        ("1", "mug", "DEMO-07"), ("3", "pen", "DEMO-10")]


def test_frames_are_revalidated_not_cached_blindly():
    c = laptop()
    c.post("/api/workspace", json={"name": "demo"})
    c.post("/api/scans", files=frames())
    r = c.get("/api/scans/S001/frames/0")
    assert r.headers["cache-control"] == "private, no-cache" and r.content == b"\xff\xd8frame"
    assert c.get("/api/scans/S001/frames/0", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert c.get("/api/scans/S001/frames/0", headers={"If-None-Match": '"other"'}).status_code == 200


def test_scan_is_dropped_if_the_listing_changes_while_it_is_analysed():
    holder = {}

    async def swapping(imgs, smp):
        holder["client"].post("/api/engagement/sample",
                              files={"file": ("new.csv", b"item_id,description\nZ,Other\n", "text/csv")})
        return SceneResult(objects=[DetectedObject(name="mug", count=1, confidence=0.9, matched_item_id="A")])

    c = holder["client"] = laptop(swapping)
    c.post("/api/engagement/sample", files={"file": ("l.csv", b"item_id,description\nA,Mug\n", "text/csv")})
    assert c.post("/api/scans", files=frames()).status_code == 409
    assert c.get("/api/state").json()["scans"] == []


# ----------------------------------------------------------------------------- exports


EVIL = '=HYPERLINK("http://evil.example","click")'
EVIL_COLUMN = "=cmd|'/c calc'!A0+"                # a listing column name is text from the audited party too
EVIL_INSIDE = "Bin 4;=cmd|'/c calc'!A0;x"         # harmless first character, formula after a semicolon
FORMULA = ("=", "+", "-", "@")


def test_exports_never_contain_live_formulas():
    async def detector(imgs, smp):
        return SceneResult(notes=EVIL, objects=[
            DetectedObject(name=EVIL, count=1, confidence=0.9, matched_item_id="A", notes="+1+1",
                           attributes=[Attribute(name="Serial number", value=EVIL),
                                       Attribute(name="Asset tag", value="T-1\t@SUM(1)")]),
            DetectedObject(name="@SUM(1)", count=1, confidence=0.5)])

    listing = (f'item_id,description,serial_number,location,"{EVIL_COLUMN}"\n'
               f'A,"{EVIL.replace(chr(34), chr(34) * 2)}",,{EVIL_INSIDE},x\n').encode()
    c = laptop(detector)
    c.post("/api/engagement/details", json={"client": "=cmd|' /C calc'!A0", "counter": "-2+3"})
    c.post("/api/engagement/sample", files={"file": ("l.csv", listing, "text/csv")})
    c.post("/api/scans", files=frames())
    c.post("/api/items/A/review", json={"decision": "follow_up", "note": "=1+1\n=2+2"})

    import csv
    text = c.get("/api/export.csv").text
    rows = list(csv.reader(io.StringIO(text)))
    cells = [cell for row in rows for cell in row]  # the header row as well: it carries the listing's column names
    assert not [x for x in cells if x.startswith(FORMULA)]
    assert "'" + EVIL in cells and "'=1+1\n'=2+2" in cells
    assert f"'{EVIL_COLUMN} (expected)" in rows[0] and f"'{EVIL_COLUMN} result" in rows[0]
    # A spreadsheet set to a semicolon list separator ignores the commas and quotes and cuts each line at ";"
    # instead; pasted text is cut at tabs. However the file is cut up, no piece may start a formula.
    for separator in (";", "\t", ","):
        pieces = [piece.lstrip(' "') for line in text.splitlines() for piece in line.split(separator)]
        assert not [x for x in pieces if x.startswith(FORMULA)], separator
    assert "Bin 4;'=cmd|'/c calc'!A0;x" in cells

    wb = load_workbook(io.BytesIO(c.get("/api/export.xlsx").content))
    texts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                assert cell.data_type != "f", f"{ws.title}!{cell.coordinate} is a formula: {cell.value!r}"
                texts.append(cell.value)
    assert EVIL in texts and "=1+1\n=2+2" in texts and "=cmd|' /C calc'!A0" in texts  # kept as text, unchanged
    assert f"{EVIL_COLUMN} (expected)" in texts and EVIL_INSIDE in texts


def test_excel_export_survives_control_characters():
    """openpyxl refuses such characters; one of them in a label used to fail the whole export with a 500."""
    async def detector(imgs, smp):
        return SceneResult(notes="glare\x1d", objects=[
            DetectedObject(name="Lap\x0btop", count=1, confidence=0.9, matched_item_id="A", notes="\x1d",
                           attributes=[Attribute(name="Serial number", value="C02\x00XK1\x1dZZ")]),
            DetectedObject(name="\x1d", count=1, confidence=0.5, attributes=[Attribute(name="Tag\x0b", value="\x1f")])])

    c = laptop(detector)
    c.post("/api/engagement/details", json={"client": "Acme\x1d Ltd", "counter": "R.\x0bAuditor"})
    listing = b"item_id,description,serial_number\nA,Lap\x0btop,C02\x1dXK1ZZ\n"
    c.post("/api/engagement/sample", files={"file": ("l.csv", listing, "text/csv")})
    assert c.get("/api/state").json()["sample"]["items"][0]["description"] == "Laptop"  # cleaned when read
    c.post("/api/scans", files=frames())
    c.post("/api/items/A/review", json={"decision": "accepted", "note": "seen\x1d by RB"})
    r = c.get("/api/export.xlsx")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    texts = [cell.value for ws in wb.worksheets for row in ws.iter_rows() for cell in row
             if isinstance(cell.value, str)]
    assert "Laptop" in texts and "C02XK1ZZ" in texts and "seen by RB" in texts and "Acme Ltd" in texts
    assert not [t for t in texts if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in t)]
    assert c.get("/api/export.csv").status_code == 200
    # The same through the engine, which is what `check --out results.xlsx` uses.
    s = sample.parse_sample(b"item_id,description\nA,Mug\n")
    assessment = sample.assess(s, [{"id": "S001", "objects": [{"name": "m\x0bug", "count": 1, "item_id": None}]}])
    assert export.results_xlsx(s, assessment, {"client": "\x1d"}, [], "label\x0b")[:2] == b"PK"


def test_csv_safe_leaves_ordinary_values_alone():
    assert export.csv_safe("C02XK1ZZJG5H") == "C02XK1ZZJG5H" and export.csv_safe(-3) == -3
    assert export.csv_safe("") == "" and export.csv_safe("-3") == "'-3"
    for ordinary in ("A-1001", "Serial number: C02X; Model number: A2337", "R&D; lab 2", "x = y", "a@b.example",
                     "line one\nline two", "tab\there"):
        assert export.csv_safe(ordinary) == ordinary


def test_csv_safe_neutralises_a_formula_wherever_a_cell_can_start():
    safe = export.csv_safe
    assert safe("Laptop;=1+1;x") == "Laptop;'=1+1;x" and safe("a; -5; +6") == "a; '-5; '+6"
    assert safe("x\t@SUM(1)") == "x\t'@SUM(1)" and safe("x\r\n=1") == "x\r\n'=1"
    # A value that starts with a tab keeps its leading apostrophe, and the formula behind the tab gets one too
    # (the old answer, "'\t=1", still left "=1" at the start of a cell once the text was cut at the tab).
    assert safe("\t=1") == "'\t'=1"


# ----------------------------------------------------------------------------- listing parsing


def test_listing_row_column_and_cell_limits(monkeypatch):
    monkeypatch.setattr(sample, "MAX_ROWS", 50)
    rows = "item_id,description\n" + "".join(f"I{n},Thing\n" for n in range(60))
    with pytest.raises(ValueError, match="more than 50 rows"):
        sample.parse_sample(rows.encode(), "big.csv")

    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["item_id", "description"])
    for n in range(60):
        wb.active.append([f"I{n}", "Thing"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="more than 50 rows"):
        sample.parse_sample(buf.getvalue(), "big.xlsx")

    wide = ",".join(["item_id", "description"] + [f"c{n}" for n in range(200)]) + "\n" + ",".join(["A", "x" * 5000]) + "\n"
    parsed = sample.parse_sample(wide.encode(), "wide.csv")
    assert len(parsed.attribute_names) == sample.MAX_COLUMNS - 2
    assert len(parsed.items[0].description) == sample.MAX_CELL_CHARS


def xlsx_with(extra: dict[str, bytes], content_types: str = "") -> bytes:
    """A small, valid listing workbook with parts added or replaced, and extra content-type declarations."""
    wb = Workbook()
    wb.active.append(["item_id", "description", "serial_number"])
    wb.active.append(["A-1", "Laptop", "C02XK1ZZ"])
    plain = io.BytesIO()
    wb.save(plain)
    out = io.BytesIO()
    with zipfile.ZipFile(plain) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = extra.get(info.filename, zin.read(info.filename))
            if info.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", content_types.encode() + b"</Types>")
            zout.writestr(info.filename, data)
        for name in extra.keys() - set(zin.namelist()):
            zout.writestr(name, extra[name])
    return out.getvalue()


def test_listing_text_is_capped_in_total(monkeypatch):
    """Rows, columns and cell length were each limited, yet together they allowed 150 million characters, all
    of it sent to the laptop page several times over on every refresh."""
    monkeypatch.setattr(sample, "MAX_LISTING_CHARS", 3000)
    header = "item_id,description," + ",".join(f"c{n}" for n in range(10))
    fits = header + "\n" + "".join(f"I{n},Thing," + ",".join(["x" * 12] * 10) + "\n" for n in range(15))
    assert len(sample.parse_sample(fits.encode(), "ok.csv").items) == 15
    over = header + "\n" + "".join(f"I{n},Thing," + ",".join(["x" * 12] * 10) + "\n" for n in range(40))
    with pytest.raises(ValueError, match=r"more than 3,000 characters of text"):
        sample.parse_sample(over.encode(), "big.csv")

    wb = Workbook()
    wb.active.append(["item_id", "description", "note"])
    for n in range(40):
        wb.active.append([f"I{n}", "Thing", "y" * 400])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match=r"more than 3,000 characters of text"):
        sample.parse_sample(buf.getvalue(), "big.xlsx")

    # Column names are repeated for every item on the page, so they count once per item: long names on many
    # short rows are refused, the same names on a few rows are fine.
    long_names = "item_id,description," + ",".join(f"{'n' * 90}{n}" for n in range(5))
    assert len(sample.parse_sample((long_names + "\nA,x\nB,y\n").encode(), "few.csv").items) == 2
    with pytest.raises(ValueError, match="characters of text"):
        sample.parse_sample((long_names + "\n" + "".join(f"I{n},x\n" for n in range(20))).encode(), "many.csv")


def test_listing_at_the_documented_limits_is_refused_quickly_not_served():
    """4,999 rows x 60 columns of one 500-character text was an 0.8 MB upload (as .xlsx) that made every
    refresh 443 MB. Two hundred such rows are already over the limit."""
    text = "Z" * sample.MAX_CELL_CHARS
    row = ",".join([text] * (sample.MAX_COLUMNS - 1))
    listing = ("item_id," + ",".join(f"c{n}" for n in range(sample.MAX_COLUMNS - 1)).replace("c0", "description")
               + "\n" + "".join(f"I{n},{row}\n" for n in range(200)))
    c = laptop()
    r = c.post("/api/engagement/sample", files={"file": ("l.csv", listing.encode(), "text/csv")})
    assert r.status_code == 400 and "4,000,000 characters" in r.json()["detail"]
    assert c.get("/api/state").json()["sample"] is None


def test_workbook_parts_that_are_loaded_whole_are_capped():
    """A 183 KB upload whose styles part unpacked to 63 MB took over 20 seconds and 1 GB or more to open."""
    assert sample.MAX_XLSX_STYLES_BYTES < sample.MAX_XLSX_SHARED_STRINGS_BYTES < sample.MAX_XLSX_UNPACKED_BYTES
    assert len(sample.parse_sample(xlsx_with({}), "plain.xlsx").items) == 1

    def padded(limit: int) -> bytes:
        return b"<x>" + b" " * limit + b"</x>"

    strings_type = ('<Override PartName="/xl/{}" ContentType="application/vnd.openxmlformats-officedocument.'
                    'spreadsheetml.sharedStrings+xml" />')
    bombs = {
        "styles": xlsx_with({"xl/styles.xml": padded(sample.MAX_XLSX_STYLES_BYTES)}),
        "shared strings": xlsx_with({"xl/sharedStrings.xml": padded(sample.MAX_XLSX_SHARED_STRINGS_BYTES)},
                                    strings_type.format("sharedStrings.xml")),
        # The reader finds the text table through the content types, so another name must not get around it.
        "renamed shared strings": xlsx_with({"xl/media/logo.png": padded(sample.MAX_XLSX_SHARED_STRINGS_BYTES)},
                                            strings_type.format("media/logo.png")),
        "content types": xlsx_with({}, " " * sample.MAX_XLSX_META_BYTES),
        "workbook": xlsx_with({"xl/workbook.xml": padded(sample.MAX_XLSX_META_BYTES)}),
        "relationships": xlsx_with({"xl/worksheets/_rels/sheet1.xml.rels": padded(sample.MAX_XLSX_META_BYTES)}),
        "properties": xlsx_with({"docProps/core.xml": padded(sample.MAX_XLSX_META_BYTES)}),
        # A sheet is streamed, but one row of millions of cells is still built whole: about 35 MB per megabyte.
        "worksheet": xlsx_with({"xl/worksheets/sheet1.xml": padded(sample.MAX_XLSX_XML_PART_BYTES)}),
        "chart sheet": xlsx_with({"xl/chartsheets/sheet9.xml": padded(sample.MAX_XLSX_XML_PART_BYTES)}),
    }
    for what, data in bombs.items():
        assert len(data) < 64 * 1024, what  # tiny uploads
        with pytest.raises(ValueError, match="too large when unpacked"):
            sample.parse_sample(data, "bomb.xlsx")
    with pytest.raises(ValueError, match="could not be read"):
        sample.parse_sample(xlsx_with({"[Content_Types].xml": b"<Types"}), "broken.xlsx")


def test_zip_bomb_and_broken_workbooks_are_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"0" * (sample.MAX_XLSX_UNPACKED_BYTES + 1))
    assert len(buf.getvalue()) < 1024 * 1024  # tiny upload, huge once unpacked
    with pytest.raises(ValueError, match="too large when unpacked"):
        sample.parse_sample(buf.getvalue(), "bomb.xlsx")
    with pytest.raises(ValueError, match="not a valid Excel"):
        sample.parse_sample(b"PK\x03\x04 not really a zip", "broken.xlsx")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not a workbook")
    with pytest.raises(ValueError, match="could not be read"):
        sample.parse_sample(buf.getvalue(), "notexcel.xlsx")
    with pytest.raises(ValueError, match="not a number"):
        sample.parse_sample(b"item_id,description,qty\nA,Mug,1e999\n", "inf.csv")


def test_entity_expansion_protection_is_active():
    import openpyxl

    assert openpyxl.DEFUSEDXML is True  # defusedxml is a declared dependency for this reason
