"""The MAF detector on each provider, against local fakes of the providers' APIs (no credits spent).

The fakes enforce the structured-output rules the real APIs apply, so a schema either provider would
reject fails here first.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from inventory_checker import detection, providers
from inventory_checker.population import FAKE_POPULATION

ANTHROPIC_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                         "minLength", "maxLength"}

REPLY = {"objects": [
    {"name": "coffee mug", "count": 2, "confidence": 0.93, "visible_text": None, "matched_sku": "KIT-3001",
     "notes": ""},
    {"name": "stress ball", "count": 1, "confidence": 0.7, "visible_text": None, "matched_sku": "XYZ-999",
     "notes": ""},
], "image_quality": "good", "notes": ""}


def anthropic_problem(node, path="schema"):
    """Anthropic: no numeric/string limits, additionalProperties false on every object."""
    if isinstance(node, dict):
        if bad := ANTHROPIC_UNSUPPORTED & node.keys():
            return f"{path}: unsupported keyword(s) {sorted(bad)}"
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            return f"{path}: objects must set additionalProperties to false"
        for k, v in node.items():
            if p := anthropic_problem(v, f"{path}.{k}"):
                return p
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if p := anthropic_problem(v, f"{path}[{i}]"):
                return p
    return None


def openai_strict_problem(node, path="schema"):
    """OpenAI strict mode: every object lists all its properties as required, additionalProperties false."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                return f"{path}: additionalProperties must be false"
            if set(node.get("required", [])) != set(node.get("properties", {})):
                return f"{path}: all properties must be required"
        if "default" in node:
            return f"{path}: default is not allowed"
        for k, v in node.items():
            if p := openai_strict_problem(v, f"{path}.{k}"):
                return p
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if p := openai_strict_problem(v, f"{path}[{i}]"):
                return p
    return None


def test_schema_is_accepted_by_both_providers():
    assert anthropic_problem(detection.RESPONSE_SCHEMA) is None
    assert openai_strict_problem(detection.RESPONSE_SCHEMA) is None
    # Regression guard: the plain Pydantic schema breaks both rule sets.
    raw = detection.SceneResult.model_json_schema()
    assert anthropic_problem(raw) and openai_strict_problem(raw)


# ----------------------------------------------------------------------------- fake API server


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for k in ("CLAUDE_MODEL", "CLAUDE_EFFORT", "CLAUDE_MAX_TOKENS", "AI_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    state = {"requests": [], "reply": REPLY}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            out = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "body": body, "headers": dict(self.headers)})
            text = json.dumps(state["reply"])
            if "/messages" in self.path:  # Anthropic Messages API
                problem = anthropic_problem(body.get("output_config", {}).get("format", {}).get("schema", {}))
                if problem:
                    return self._send(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                       "message": problem}})
                return self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                        "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})
            # OpenAI / Azure OpenAI Chat Completions
            rf = body.get("response_format", {})
            if rf.get("json_schema", {}).get("strict"):
                problem = openai_strict_problem(rf["json_schema"]["schema"])
                if problem:
                    return self._send(400, {"error": {"message": problem, "type": "invalid_request_error"}})
            return self._send(200, {"id": "c1", "object": "chat.completion", "created": 0,
                                    "model": body.get("model", "deployment"),
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": text}}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_port}"
    yield state
    srv.shutdown()


FRAMES = [(b"\xff\xd8frame%d" % i, "image/jpeg") for i in range(4)]


def _check_result(result):
    assert [o.matched_sku for o in result.objects] == ["KIT-3001", None]  # invented SKU dropped
    assert "unknown SKU XYZ-999" in result.objects[1].notes


def test_anthropic_claude_haiku(fake):
    client = providers.build_anthropic(api_key="test-key", base_url=fake["url"])
    _check_result(asyncio.run(detection.detect(client, FRAMES, FAKE_POPULATION)))
    [req] = fake["requests"]
    body = req["body"]
    assert req["path"].startswith("/v1/messages")
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["effort"] == "low" and body["max_tokens"] == 4000
    assert body["output_config"]["format"]["schema"] == detection.RESPONSE_SCHEMA
    assert "thinking" not in body and "temperature" not in body
    content = body["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image"] * 4 + ["text"]  # images first, one request
    assert "4 consecutive video frames" in content[-1]["text"]


def test_openai(fake):
    client = providers.build_openai(api_key="sk-test", model="vision-model", base_url=fake["url"] + "/v1")
    _check_result(asyncio.run(detection.detect(client, FRAMES, FAKE_POPULATION)))
    [req] = fake["requests"]
    body = req["body"]
    assert req["path"] == "/v1/chat/completions" and body["model"] == "vision-model"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_completion_tokens"] == 4000
    # MAF's OpenAI client sends the images and the text as consecutive user messages.
    user = [m for m in body["messages"] if m["role"] == "user"]
    images = [p for m in user if isinstance(m["content"], list) for p in m["content"] if p["type"] == "image_url"]
    assert len(images) == 4 and images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "4 consecutive video frames" in json.dumps(user[-1]["content"])


def test_azure_openai(fake):
    client = providers.build_azure_openai(endpoint=fake["url"], deployment="my-vision-deployment",
                                          api_key="azure-key", api_version="2024-10-21")
    _check_result(asyncio.run(detection.detect(client, FRAMES, FAKE_POPULATION)))
    [req] = fake["requests"]
    assert req["path"].startswith("/openai/deployments/my-vision-deployment/chat/completions")
    assert "api-version=2024-10-21" in req["path"]
    assert req["headers"].get("api-key") == "azure-key"
    assert req["body"]["response_format"]["json_schema"]["schema"] == detection.RESPONSE_SCHEMA


def test_out_of_range_answer_is_clamped(fake):
    fake["reply"] = {"objects": [{"name": "mug", "count": -2, "confidence": 3, "visible_text": None,
                                  "matched_sku": None, "notes": ""}], "image_quality": "good", "notes": ""}
    client = providers.build_anthropic(api_key="test-key", base_url=fake["url"])
    [obj] = asyncio.run(detection.detect(client, FRAMES[:1], FAKE_POPULATION)).objects
    assert obj.count == 0 and obj.confidence == 1.0


def test_frames_are_capped(fake):
    fake["reply"] = {"objects": [], "image_quality": "poor", "notes": "dark"}
    client = providers.build_anthropic(api_key="test-key", base_url=fake["url"])
    asyncio.run(detection.detect(client, [(b"\xff\xd8x", "image/jpeg")] * 10, FAKE_POPULATION))
    images = [b for b in fake["requests"][0]["body"]["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == detection.MAX_FRAMES


# ----------------------------------------------------------------------------- provider selection


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("AI_PROVIDER", "ANTHROPIC_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT",
              "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL", "CLAUDE_MODEL", "DETECTOR"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_provider_auto_selection_and_override(clean_env):
    m = clean_env
    assert providers.selected_provider() is None and providers.build_chat_client() is None
    m.setenv("OPENAI_API_KEY", "sk")
    m.setenv("OPENAI_MODEL", "vision-model")
    assert providers.selected_provider().name == "openai"
    m.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    m.setenv("AZURE_OPENAI_DEPLOYMENT", "dep")
    assert providers.selected_provider().name == "azure_openai"
    m.setenv("ANTHROPIC_API_KEY", "sk-ant")
    _, name, model = providers.build_chat_client()
    assert (name, model) == ("anthropic", "claude-haiku-5-5")
    m.setenv("AI_PROVIDER", "azure-openai")
    assert providers.selected_provider().name == "azure_openai"
    m.setenv("AI_PROVIDER", "none")
    assert providers.build_chat_client() is None
    m.setenv("AI_PROVIDER", "nope")
    with pytest.raises(ValueError, match="Unknown AI_PROVIDER"):
        providers.selected_provider()


def test_register_custom_provider(clean_env):
    sentinel = object()
    providers.register_provider(providers.Provider("dummy", lambda: False, lambda: sentinel, lambda: "m"))
    try:
        clean_env.setenv("AI_PROVIDER", "dummy")
        assert providers.build_chat_client() == (sentinel, "dummy", "m")
    finally:
        providers._REGISTRY.pop("dummy")


async def my_detector(frames, population):  # used by test_custom_detector_plugin
    return detection.SceneResult(objects=[detection.DetectedObject(name="x", count=1, confidence=1)])


def test_custom_detector_plugin(clean_env):
    fn = detection.load_custom_detector("tests.test_detection:my_detector")
    assert asyncio.run(fn([], [])).objects[0].name == "x"
    with pytest.raises(ValueError):
        detection.load_custom_detector("no-colon")
