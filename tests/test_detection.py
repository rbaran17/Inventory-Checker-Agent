"""The MAF detector on Claude, against a local fake of the Messages API (no credits spent)."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from inventory_checker import detection
from inventory_checker.population import FAKE_POPULATION

UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength"}


def _schema_problem(node, path="schema"):
    """The structured-output rules the real API enforces that a plain Pydantic schema breaks."""
    if isinstance(node, dict):
        bad = UNSUPPORTED & node.keys()
        if bad:
            return f"{path}: unsupported keyword(s) {sorted(bad)}"
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            return f"{path}: objects must set additionalProperties to false"
        for k, v in node.items():
            if (p := _schema_problem(v, f"{path}.{k}")):
                return p
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if (p := _schema_problem(v, f"{path}[{i}]")):
                return p
    return None


def test_raw_pydantic_schema_would_be_rejected():
    # Regression guard: this is what the MAF connector would send without transform_schema.
    assert _schema_problem(detection.SceneResult.model_json_schema()) is not None
    assert _schema_problem(detection.response_schema()["schema"]) is None


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
            problem = _schema_problem(body.get("output_config", {}).get("format", {}).get("schema", {}))
            if problem:  # behave like the real API: unsupported structured-output schema is a 400
                out = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": problem}})
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out.encode())
                return
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


def test_out_of_range_answer_is_rejected_in_code(fake_api):
    fake_api["reply"] = {"objects": [{"name": "mug", "count": -2, "confidence": 3, "visible_text": None,
                                      "matched_sku": None, "notes": ""}], "image_quality": "good", "notes": ""}
    with pytest.raises(Exception, match="greater than or equal to 0|less than or equal to 1"):
        asyncio.run(detection.detect(fake_api["client"], [(b"\xff\xd8x", "image/jpeg")], FAKE_POPULATION))


def test_frames_are_capped(fake_api):
    fake_api["reply"] = {"objects": [], "image_quality": "poor", "notes": "dark"}
    frames = [(b"\xff\xd8x", "image/jpeg")] * 10
    asyncio.run(detection.detect(fake_api["client"], frames, FAKE_POPULATION))
    images = [b for b in fake_api["requests"][0]["body"]["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == detection.MAX_FRAMES
