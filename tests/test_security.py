"""Access control, limits, export safety and defensive listing parsing."""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from inventory_checker import export, sample, security
from inventory_checker.api import KEY_ATTEMPTS, create_app
from inventory_checker.config import Settings, load_or_create_access_key, rotate_access_key
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
    # A browser that is already signed in is not affected by someone else's guessing.
    signed_in = laptop()
    for _ in range(KEY_ATTEMPTS + 1):
        signed_in.get("/?key=guess")
    assert signed_in.get("/api/state").status_code == 200


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


def test_exports_never_contain_live_formulas():
    async def detector(imgs, smp):
        return SceneResult(notes=EVIL, objects=[
            DetectedObject(name=EVIL, count=1, confidence=0.9, matched_item_id="A", notes="+1+1",
                           attributes=[Attribute(name="Serial number", value=EVIL)]),
            DetectedObject(name="@SUM(1)", count=1, confidence=0.5)])

    listing = f'item_id,description,serial_number\nA,"{EVIL.replace(chr(34), chr(34) * 2)}",\n'.encode()
    c = laptop(detector)
    c.post("/api/engagement/details", json={"client": "=cmd|' /C calc'!A0", "counter": "-2+3"})
    c.post("/api/engagement/sample", files={"file": ("l.csv", listing, "text/csv")})
    c.post("/api/scans", files=frames())
    c.post("/api/items/A/review", json={"decision": "follow_up", "note": "=1+1"})

    import csv
    rows = list(csv.reader(io.StringIO(c.get("/api/export.csv").text)))
    cells = [cell for row in rows[1:] for cell in row]
    assert not [x for x in cells if x.startswith(("=", "+", "-", "@"))]
    assert "'" + EVIL in cells and "'=1+1" in cells

    wb = load_workbook(io.BytesIO(c.get("/api/export.xlsx").content))
    texts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                assert cell.data_type != "f", f"{ws.title}!{cell.coordinate} is a formula: {cell.value!r}"
                texts.append(cell.value)
    assert EVIL in texts and "=1+1" in texts and "=cmd|' /C calc'!A0" in texts  # kept as text, unchanged


def test_csv_safe_leaves_ordinary_values_alone():
    assert export.csv_safe("C02XK1ZZJG5H") == "C02XK1ZZJG5H" and export.csv_safe(-3) == -3
    assert export.csv_safe("") == "" and export.csv_safe("-3") == "'-3" and export.csv_safe("\t=1") == "'\t=1"


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
