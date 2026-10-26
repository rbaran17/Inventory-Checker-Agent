"""Claude via the MAF Anthropic connector, against a local fake of the Messages API."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from agent_framework import Message

from inventory_checker.agents import build_observer_copilot, read_evidence
from inventory_checker.agents import client as client_mod

from .conftest import OBS


def _msg(content, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    state = {"requests": [], "script": []}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "body": body, "headers": dict(self.headers)})
            out = json.dumps(state["script"].pop(0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    claude_cls, _ = client_mod._claude_client_classes()
    state["client"] = claude_cls(model="claude-opus-5-5", api_key="test-key",
                                 base_url=f"http://127.0.0.1:{srv.server_port}")
    yield state
    srv.shutdown()


def test_provider_selection(monkeypatch):
    for k in ("AI_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_FOUNDRY_RESOURCE", "AZURE_OPENAI_ENDPOINT",
              "AZURE_OPENAI_DEPLOYMENT", "OPENAI_API_KEY", "OPENAI_MODEL", "CLAUDE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert client_mod.provider() is None and client_mod.build_chat_client() is None
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt")
    assert client_mod.provider() == "azure"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert client_mod.provider() == "claude" and client_mod.model_name() == "claude-opus-5-5"
    assert type(client_mod.build_chat_client()).__name__ == "ClaudeClient"
    monkeypatch.setenv("AI_PROVIDER", "azure")
    assert client_mod.model_name() == "gpt"
    monkeypatch.setenv("AI_PROVIDER", "none")
    assert client_mod.build_chat_client() is None


def test_evidence_reader_on_claude_sends_image_and_schema(fake_api):
    extract = {"tag_numbers": ["1043"], "skus": ["FG-2002"], "lot_codes": [], "bin_code": None, "uom_hint": None,
               "suggested_count": 48, "count_basis": "6 x 8", "blurry": False, "glare": False, "quality": "good",
               "confidence": 0.9, "notes": ""}
    fake_api["script"] = [_msg([{"type": "text", "text": json.dumps(extract)}])]
    out = asyncio.run(read_evidence(fake_api["client"], b"\xff\xd8jpeg", "image/jpeg"))
    assert out.suggested_count == 48 and out.skus == ["FG-2002"]
    req = fake_api["requests"][0]
    body = req["body"]
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")
    assert "temperature" not in body and "thinking" not in body
    blocks = body["messages"][0]["content"]
    assert any(b["type"] == "image" and b["source"]["media_type"] == "image/jpeg" for b in blocks)
    assert "auditor" in json.dumps(body["system"])


def test_copilot_on_claude_tool_approval_round_trip(fake_api, service, session):
    fake_api["script"] = [
        _msg([{"type": "tool_use", "id": "toolu_1", "name": "record_test_count",
               "input": {"sku": "FG-2002", "auditor_count": 255, "direction": "floor_to_sheet"}}], stop="tool_use"),
        _msg([{"type": "text", "text": "Saved."}]),
    ]
    agent = build_observer_copilot(fake_api["client"], service, session.id, OBS)
    s = agent.create_session()
    resp = asyncio.run(agent.run("Counted 255 of FG-2002", session=s))
    [req] = resp.user_input_requests
    assert service.records(session.id) == []
    tools = {t["name"] for t in fake_api["requests"][0]["body"]["tools"]}
    assert {"record_test_count", "list_open_items"} <= tools
    assert fake_api["requests"][0]["body"].get("tool_choice", {"type": "auto"})["type"] == "auto"
    asyncio.run(agent.run(Message(role="user", contents=[req.to_function_approval_response(True)]), session=s))
    [rec] = service.records(session.id)
    assert rec.data["difference"] == -5
    second = fake_api["requests"][1]["body"]["messages"]
    results = [b for m in second if isinstance(m["content"], list) for b in m["content"] if b["type"] == "tool_result"]
    assert results and results[0]["tool_use_id"] == "toolu_1"
