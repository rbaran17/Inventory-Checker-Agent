"""The MAF detector on Claude, against a local fake of the Messages API (no credits spent)."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from inventory_checker import detection
from inventory_checker.population import FAKE_POPULATION


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for k in ("CLAUDE_MODEL", "CLAUDE_EFFORT", "CLAUDE_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    state = {"requests": [], "reply": None}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"body": body, "headers": dict(self.headers)})
            out = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                              "content": [{"type": "text", "text": json.dumps(state["reply"])}],
                              "stop_reason": "end_turn", "stop_sequence": None,
                              "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["client"] = detection.build_client(api_key="test-key", base_url=f"http://127.0.0.1:{srv.server_port}")
    yield state
    srv.shutdown()


def test_no_key_means_no_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert detection.build_client() is None


def test_frames_go_to_haiku_in_one_cheap_call(fake_api):
    fake_api["reply"] = {"objects": [
        {"name": "coffee mug", "count": 2, "confidence": 0.93, "visible_text": None, "matched_sku": "KIT-3001",
         "notes": ""},
        {"name": "stress ball", "count": 1, "confidence": 0.7, "visible_text": None, "matched_sku": "XYZ-999",
         "notes": ""},
    ], "image_quality": "good", "notes": ""}
    frames = [(b"\xff\xd8frame%d" % i, "image/jpeg") for i in range(4)]
    result = asyncio.run(detection.detect(fake_api["client"], frames, FAKE_POPULATION))

    assert [o.matched_sku for o in result.objects] == ["KIT-3001", None]  # invented SKU dropped
    assert "unknown SKU XYZ-999" in result.objects[1].notes
    [req] = fake_api["requests"]
    body = req["body"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low" and body["max_tokens"] == 4000
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body and "temperature" not in body and "fallbacks" not in body
    content = body["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image"] * 4 + ["text"]  # images first, one request
    assert "KIT-3001" in content[-1]["text"] and "4 consecutive video frames" in content[-1]["text"]


def test_frames_are_capped(fake_api):
    fake_api["reply"] = {"objects": [], "image_quality": "poor", "notes": "dark"}
    frames = [(b"\xff\xd8x", "image/jpeg")] * 10
    asyncio.run(detection.detect(fake_api["client"], frames, FAKE_POPULATION))
    images = [b for b in fake_api["requests"][0]["body"]["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == detection.MAX_FRAMES
