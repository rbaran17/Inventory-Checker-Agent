from __future__ import annotations

from fastapi.testclient import TestClient

from inventory_checker.api import create_app
from inventory_checker.config import Settings
from inventory_checker.detection import DetectedObject, SceneResult

KEY = "test-key-123"


def make(detector=None, use_env_client=False):
    app = create_app(Settings(public_base_url="https://tunnel.example", access_key=KEY), detector=detector,
                     use_env_client=use_env_client)
    return TestClient(app)


def authed(detector=None):
    c = make(detector)
    r = c.get(f"/?key={KEY}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/"
    return c


def test_access_key_required():
    c = make()
    assert c.get("/").status_code == 401
    assert c.get("/api/state").status_code == 401
    assert c.get("/?key=wrong").status_code == 401
    c = authed()
    assert "Inventory Count POC" in c.get("/").text and "Count Camera" in c.get("/scan").text
    qr = c.get("/api/qr").json()
    assert qr["url"] == f"https://tunnel.example/scan?key={KEY}" and "<svg" in qr["svg"]


def test_scan_flow_end_to_end():
    calls = []

    async def fake_detector(frames, population):
        calls.append(len(frames))
        return SceneResult(objects=[
            DetectedObject(name="laptop", count=1, confidence=0.95, matched_sku="OFF-1001"),
            DetectedObject(name="pen", count=5, confidence=0.8, matched_sku="STA-2001"),
            DetectedObject(name="rubber duck", count=1, confidence=0.6),
        ], image_quality="good")

    c = authed(fake_detector)
    s = c.get("/api/state").json()
    assert s["ai_ready"] and len(s["population"]) == 35
    s = c.post("/api/sample", json={"size": 3, "value_threshold": 500, "seed": 1}).json()
    sampled = {p["sku"] for p in s["population"] if p["sampled"]}
    assert "OFF-1001" in sampled  # laptop is worth 1,400: always sampled

    assert c.post("/api/ping").json()["ok"]
    assert c.get("/api/state").json()["phone"]["connected"]

    files = [("frames", (f"f{i}.jpg", b"\xff\xd8frame", "image/jpeg")) for i in range(4)]
    r = c.post("/api/scans", files=files).json()
    assert calls == [4] and r["scan"]["id"] == "scan_001" and r["scan"]["frames"] == 4
    rows = {x["sku"]: x for x in r["assessment"]["sample"]}
    assert rows["OFF-1001"]["status"] == "match"
    assert c.get("/api/scans/scan_001/frames/3").content == b"\xff\xd8frame"

    # correct a wrong mapping by hand
    s = c.post("/api/scans/scan_001/objects/2", json={"sku": "HOM-4002"}).json()
    assert s["scans"][0]["objects"][2]["mapped_by"] == "you"
    assert c.post("/api/scans/scan_001/objects/2", json={"sku": "NOPE"}).status_code == 400

    assert c.post("/api/scans", files=[("frames", ("x.gif", b"GIF8", "image/gif"))]).status_code == 400
    assert c.delete("/api/scans").json()["scans"] == []


def test_without_api_key_scans_are_refused_clearly():
    c = authed(None)
    assert c.get("/api/state").json()["ai_ready"] is False
    r = c.post("/api/scans", files=[("frames", ("f.jpg", b"\xff\xd8", "image/jpeg"))])
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_population_upload_redraws_sample_and_clears_stale_matches():
    async def det(frames, population):
        return SceneResult(objects=[DetectedObject(name="mug", count=2, confidence=0.9, matched_sku="KIT-3001")])

    c = authed(det)
    c.post("/api/scans", files=[("frames", ("f.jpg", b"\xff\xd8", "image/jpeg"))])
    s = c.post("/api/population", json={"csv": "sku,description,location,book_qty,unit_cost\nW1,Mug,Shelf,2,5\n"}).json()
    assert [p["sku"] for p in s["population"]] == ["W1"]
    assert s["scans"][0]["objects"][0]["matched_sku"] is None
    assert c.post("/api/population", json={"csv": "bad"}).status_code == 400


def test_access_key_survives_restarts(monkeypatch, tmp_path):
    from inventory_checker.config import load_or_create_access_key

    monkeypatch.delenv("APP_ACCESS_KEY", raising=False)
    monkeypatch.setenv("APP_ACCESS_KEY_FILE", str(tmp_path / "key"))
    first = load_or_create_access_key()
    assert load_or_create_access_key() == first and (tmp_path / "key").stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("APP_ACCESS_KEY", "fixed")
    assert load_or_create_access_key() == "fixed"
