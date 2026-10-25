from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message

from inventory_checker.service import ObservationService
from inventory_checker.store import Database, LocalWormEvidenceStore

LEAD = "lead@firm.example"
OBS = "observer@firm.example"
LISTING = (Path(__file__).resolve().parents[1] / "samples" / "perpetual_listing.csv").read_text()


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
    """Chat client that replays scripted model turns, so MAF agents run without a live model."""

    def __init__(self, script: list[ChatResponse]):
        super().__init__()
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.calls.append({"messages": list(messages), "options": dict(options)})
        if not self.script:
            return ChatResponse(messages=[Message(role="assistant", contents=["(no more script)"])])
        return self.script.pop(0)


def text_turn(text: str) -> ChatResponse:
    return ChatResponse(messages=[Message(role="assistant", contents=[text])])


def tool_turn(name: str, args: dict[str, Any], call_id: str = "call_1") -> ChatResponse:
    return ChatResponse(messages=[Message(role="assistant", contents=[
        Content.from_function_call(call_id=call_id, name=name, arguments=json.dumps(args))])])


@pytest.fixture
def service(tmp_path) -> ObservationService:
    return ObservationService(Database(), LocalWormEvidenceStore(tmp_path / "evidence"))


@pytest.fixture
def session(service):
    s = service.create_session(LEAD, engagement="FY26", client="Acme", location="WH1", count_date="2026-12-31",
                               tolerance_units=1, tolerance_pct=2, sample_size=2, sample_value_threshold=50000)
    service.add_zone(LEAD, s.id, "A")
    service.add_zone(LEAD, s.id, "B")
    service.import_listing(LEAD, s.id, LISTING)
    return s


@pytest.fixture
def device(service, session):
    token, _ = service.create_pairing_token(LEAD, session.id)
    dev, _bearer = service.redeem_pairing(OBS, token)
    return dev
