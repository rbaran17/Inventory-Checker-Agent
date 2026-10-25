from __future__ import annotations

import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from inventory_checker.api import create_app
from inventory_checker.config import Settings
from inventory_checker.store import sha256_hex

from .conftest import LEAD, LISTING, OBS

L = {"X-User": LEAD}
O = {"X-User": OBS}


def make_client(tmp_path, service=None):
    st = Settings(data_dir=tmp_path, public_base_url="https://observe.test")
    return TestClient(create_app(st, service=service, use_env_client=False))


def test_end_to_end_capture_to_export(tmp_path):
    c = make_client(tmp_path)
    assert c.get("/api/me").status_code == 401
    s = c.post("/api/sessions", headers=L, json={"engagement": "FY26", "client": "Acme", "location": "WH1",
                                                  "count_date": "2026-12-31", "tolerance_units": 1}).json()
    sid = s["id"]
    assert c.post(f"/api/sessions/{sid}/zones", headers=L, json={"code": "B"}).status_code == 200
    assert c.post(f"/api/sessions/{sid}/listing", headers=L, json={"csv": LISTING}).json() == {"imported": 13}

    pairing = c.post(f"/api/sessions/{sid}/pairing", headers=L).json()
    assert pairing["url"].startswith("https://observe.test/pair?token=") and "<svg" in pairing["qr_svg"]
    token = pairing["url"].split("token=")[1]
    paired = c.post("/api/pair/redeem", headers=O, json={"token": token}).json()
    bearer = {"Authorization": f"Bearer {paired['device_token']}"}
    assert paired["session"]["client"] == "Acme"
    assert c.post("/api/pair/redeem", headers=O, json={"token": token}).status_code == 403

    rec = c.post(f"/api/sessions/{sid}/records", headers=O, json={
        "type": "test_count", "data": {"sku": "FG-2001", "auditor_count": 40, "direction": "floor_to_sheet",
                                       "zone": "B", "tag_no": "1043"}}).json()
    assert rec["data"]["difference"] == -2 and rec["data"]["over_tolerance"] is True
    assert c.get("/api/device/me", headers=bearer).json()["active_record"]["id"] == rec["id"]

    img = b"\xff\xd8\xff\xe0 photo of pumps"
    bad = c.post("/api/capture", headers=bearer, files={"file": ("x.jpg", img, "image/jpeg")},
                 data={"sha256": "0" * 64, "kind": "photo", "capture_time": "2026-12-31T10:00:00Z"})
    assert bad.status_code == 422
    ok = c.post("/api/capture", headers=bearer, files={"file": ("x.jpg", img, "image/jpeg")},
                data={"sha256": sha256_hex(img), "kind": "photo", "capture_time": "2026-12-31T10:00:00Z",
                      "zone": "B"}).json()
    assert ok["record_ids"] == [rec["id"]]  # attached to the record open on the laptop
    content = c.get(f"/api/evidence/{ok['evidence_id']}/content", headers=O)
    assert content.content == img and content.headers["x-content-sha256"] == sha256_hex(img)

    assert c.post("/api/device/heartbeat", headers=bearer, json={"battery_level": 0.5, "queued_uploads": 1}).json()["ok"]
    r = c.post(f"/api/sessions/{sid}/close", headers=O, json={})
    assert r.status_code == 409 and r.json()["status"] == "blocked"
    c.post("/api/device/heartbeat", headers=bearer, json={"battery_level": 0.5, "queued_uploads": 0})
    c.post(f"/api/sessions/{sid}/records", headers=O, json={
        "type": "tag_control", "data": {"range_start": 1000, "range_end": 1100, "used": ["1000-1090"],
                                        "unused": ["1091-1100"]}})
    r = c.post(f"/api/sessions/{sid}/close", headers=O, json={})
    assert r.status_code == 409 and any(i["kind"] == "unexplained_difference" for i in r.json()["open_items"])
    r = c.post(f"/api/records/{rec['id']}/corrections", headers=O,
               json={"changes": {"explanation": "2 pumps staged on dock for shipment SO-551"}, "reason": "add explanation"})
    assert r.status_code == 200
    r = c.post(f"/api/sessions/{sid}/close", headers=O, json={})
    assert r.status_code == 200, r.json()
    # closed: phone uploads refused, device unpaired
    assert c.get("/api/device/me", headers=bearer).status_code == 403

    xlsx = c.get(f"/api/sessions/{sid}/export/schedule.xlsx", headers=L)
    wb = load_workbook(io.BytesIO(xlsx.content))
    assert wb["Test Counts"].cell(2, 5).value == "FG-2001"
    assert c.get(f"/api/sessions/{sid}/export/schedule.csv", headers=L).text.startswith("record_id,")
    assert c.get(f"/api/sessions/{sid}/export/package.zip", headers=L).headers["content-type"] == "application/zip"
    audit = c.get(f"/api/sessions/{sid}/audit", headers=L).json()
    assert audit["chain_valid"] and any(e["action"] == "session.close" for e in audit["events"])

    for rr in c.get(f"/api/sessions/{sid}/records", headers=L).json():
        assert c.post(f"/api/records/{rr['id']}/review", headers=L, json={"action": "approve"}).status_code == 200
    assert c.post(f"/api/sessions/{sid}/sign-off", headers=L).json()["status"] == "reviewed"


def test_outsiders_cannot_read_session(tmp_path):
    c = make_client(tmp_path)
    sid = c.post("/api/sessions", headers=L, json={"engagement": "E", "client": "C", "location": "L",
                                                    "count_date": "2026-12-31"}).json()["id"]
    assert c.get(f"/api/sessions/{sid}", headers={"X-User": "stranger@firm.example"}).status_code == 403


def test_ai_endpoints_report_unconfigured(tmp_path):
    c = make_client(tmp_path)
    sid = c.post("/api/sessions", headers=L, json={"engagement": "E", "client": "C", "location": "L",
                                                    "count_date": "2026-12-31"}).json()["id"]
    assert c.post(f"/api/sessions/{sid}/copilot", headers=L, json={"message": "hi"}).status_code == 503


def test_pages_served(tmp_path):
    c = make_client(tmp_path)
    assert "Inventory Observation" in c.get("/").text
    assert "Count Camera" in c.get("/pair").text


def test_dev_cookie_sign_in_is_url_decoded_and_observers_planned(tmp_path):
    c = make_client(tmp_path)
    sid = c.post("/api/sessions", headers=L, json={"engagement": "E", "client": "C", "location": "L",
                                                    "count_date": "2026-12-31", "observers": [OBS]}).json()["id"]
    c.cookies.set("ic_user", "observer%40firm.example")
    assert c.get("/api/me").json()["user"] == OBS
    assert c.get(f"/api/sessions/{sid}").status_code == 200
