from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from inventory_checker.agents import build_observer_copilot, draft_memo, suggest_plan
from inventory_checker.api import create_app
from inventory_checker.config import Settings
from inventory_checker.knowledge import (
    GROUNDING_RULES,
    HttpKnowledgeSource,
    SkillFolderSource,
    build_guidance_tool,
    knowledge_from_env,
    standin_source,
)
from inventory_checker.service import Forbidden, ServiceError

from .conftest import LEAD, LISTING, OBS, ScriptedChatClient, text_turn, tool_turn


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ sources
def test_standin_is_searchable_and_marked_placeholder():
    k = standin_source()
    [top, *_] = k.search("floor to sheet and sheet to floor test counts", topic="test_counts")
    assert top.id == "test-counts#test-counts-in-both-directions"
    assert "ISA 501.4(a)(iv)" in top.refs and "test_counts" in top.topics
    assert top.authoritative is False
    assert all("test_counts" in h.topics for h in k.search("counts", topic="test_counts", top_k=10))


def test_skill_folder_source_reads_skill_layout(tmp_path):
    (tmp_path / "references").mkdir()
    (tmp_path / "SKILL.md").write_text("---\nname: firm-audit-knowledge\n---\n# Overview\nFirm audit methodology.\n")
    (tmp_path / "references" / "inv.md").write_text(
        "# Roll-forward procedures\n> refs: FIRM-INV-4.2\n> topics: planning\n\nTest movements between count date "
        "and year end.\n")
    k = SkillFolderSource(tmp_path)
    [hit] = k.search("roll-forward count date year end", top_k=1)
    assert hit.id == "inv#roll-forward-procedures" and hit.refs == ["FIRM-INV-4.2"] and hit.authoritative
    assert "name:" not in " ".join(s.text for s in k.snippets)


def test_knowledge_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDIT_KNOWLEDGE_SOURCE", raising=False)
    assert knowledge_from_env().name == "standin"
    monkeypatch.setenv("AUDIT_KNOWLEDGE_SOURCE", "none")
    assert knowledge_from_env() is None
    (tmp_path / "a.md").write_text("# T\nbody\n")
    monkeypatch.setenv("AUDIT_KNOWLEDGE_SOURCE", f"dir:{tmp_path}")
    assert isinstance(knowledge_from_env(), SkillFolderSource)
    monkeypatch.setenv("AUDIT_KNOWLEDGE_SOURCE", "http:https://kb.firm.example/search")
    src = knowledge_from_env()
    assert isinstance(src, HttpKnowledgeSource) and src.url == "https://kb.firm.example/search"
    monkeypatch.setenv("AUDIT_KNOWLEDGE_SOURCE", "ftp:x")
    with pytest.raises(ValueError):
        knowledge_from_env()


def test_http_source_contract():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["auth"] = self.headers.get("Authorization")
            out = json.dumps({"results": [{"id": "M-12", "title": "Test counts", "text": "Count both ways.",
                                           "source": "methodology/inventory", "refs": ["AS 2510"], "score": 3}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        k = HttpKnowledgeSource(f"http://127.0.0.1:{srv.server_port}/search", token="t0k")
        [hit] = k.search("test counts", topic="test_counts", top_k=3)
    finally:
        srv.shutdown()
    assert hit.id == "M-12" and hit.refs == ["AS 2510"] and hit.authoritative
    assert seen["body"] == {"query": "test counts", "top_k": 3, "topic": "test_counts", "domain": "inventory"}
    assert seen["auth"] == "Bearer t0k"


def test_guidance_tool_logs_hits_and_survives_failures():
    log = []
    t = build_guidance_tool(standin_source(), log=log)
    out = json.loads(run(t.invoke(query="tag control unused tags"))[0].text)
    assert out["results"][0]["id"] == "cutoff-tags#tag-control" and log[0].id == "cutoff-tags#tag-control"

    class Broken:
        name = "broken"

        def search(self, *a, **k):
            raise RuntimeError("service down")

    out = json.loads(run(build_guidance_tool(Broken()).invoke(query="x"))[0].text)
    assert "service down" in out["error"] and out["results"] == []


# ------------------------------------------------------------------ copilot grounding
def test_copilot_searches_guidance_without_approval_and_collects_citations(service, session):
    client = ScriptedChatClient([
        tool_turn("search_audit_guidance", {"query": "count while shipping continues", "topic": "procedures"}),
        text_turn("Confirm how moving goods are kept out of the count [G:count-procedures#movement-freeze-and-segregation]."),
    ])
    cites = []
    agent = build_observer_copilot(client, service, session.id, OBS, knowledge=standin_source(), citations=cites)
    resp = run(agent.run("They are still shipping, what do I do?"))
    assert resp.user_input_requests == []
    assert "count-procedures#movement-freeze-and-segregation" in {c.id for c in cites}
    assert GROUNDING_RULES.splitlines()[0] in client.calls[0]["options"]["instructions"]
    assert "search_audit_guidance" in {t.name for t in client.calls[0]["options"]["tools"]}


def test_copilot_without_knowledge_has_no_guidance_tool(service, session):
    client = ScriptedChatClient([text_turn("Please ask the engagement lead.")])
    run(build_observer_copilot(client, service, session.id, OBS).run("what sample size?"))
    assert "search_audit_guidance" not in {t.name for t in client.calls[0]["options"]["tools"]}
    assert "No audit methodology source" in client.calls[0]["options"]["instructions"]


# ------------------------------------------------------------------ memo grounding
def test_memo_cites_guidance_for_sections_present(service, session):
    service.create_record(OBS, session.id, "test_count", {"sku": "FG-2002", "auditor_count": 260,
                                                         "direction": "floor_to_sheet"})
    memo = run(draft_memo(service, session.id, knowledge=standin_source()))
    refs = memo.split("## Methodology references", 1)[1]
    assert "[G:test-counts#test-counts-in-both-directions]" in refs
    assert "cutoff" not in refs.lower()  # no cutoff records, so no cutoff guidance
    assert "placeholder" in refs


def test_memo_summary_drops_citations_it_was_not_given(service, session):
    service.create_record(OBS, session.id, "test_count", {"sku": "FG-2002", "auditor_count": 260,
                                                         "direction": "floor_to_sheet"})
    client = ScriptedChatClient([text_turn("Counts done [G:test-counts#test-counts-in-both-directions]. "
                                           "Also [G:made-up#rule].")])
    memo = run(draft_memo(service, session.id, client, knowledge=standin_source()))
    summary = memo.split("## Summary", 1)[1].split("\n## ", 1)[0]
    assert "[G:test-counts#test-counts-in-both-directions]" in summary
    assert "made-up" not in summary and "[citation not found]" in summary
    sent = json.loads(client.calls[0]["messages"][-1].text)
    assert any(g["id"] == "test-counts#test-counts-in-both-directions" for g in sent["guidance"])


# ------------------------------------------------------------------ planning grounding
def test_plan_checklist_without_model_cites_real_snippets(service, session):
    out = run(suggest_plan(service, session.id, standin_source()))
    assert out["generated_by"] == "checklist"
    ids = {g["id"] for g in out["guidance"]}
    cited = {c for i in out["plan"]["procedures_to_observe"] for c in i["citations"]}
    assert cited and cited <= ids
    assert out["plan"]["sampling"]["value_threshold"] > 0
    assert out["facts"]["listing_items"] == 13
    assert any(e["action"] == "plan.suggest" for e in service.db.list_audit(session.id))


def test_plan_ai_draft_drops_unretrieved_citations(service, session):
    draft = {
        "procedures_to_observe": [{"item": "Observe count teams", "citations": [
            "count-procedures#observing-the-performance-of-count-procedures", "G:invented#x"]}],
        "sampling": {"approach": "Threshold plus random", "random_sample_size": 8, "value_threshold": 60000,
                     "citations": ["test-counts#selecting-items-for-test-counts"]},
        "tolerance": {"units": 1, "pct": 2, "rationale": "team judgement", "citations": []},
        "open_questions": ["Firm tolerance?"],
    }
    client = ScriptedChatClient([text_turn(json.dumps(draft))])
    out = run(suggest_plan(service, session.id, standin_source(), client))
    assert out["generated_by"] == "ai_draft" and out["dropped_citations"] == ["invented#x"]
    assert out["plan"]["procedures_to_observe"][0]["citations"] == [
        "count-procedures#observing-the-performance-of-count-procedures"]
    assert service.get_session(session.id).sample_size == 2  # suggestion is not applied by itself


def test_update_plan_is_lead_only_and_audited(service, session):
    with pytest.raises(Forbidden):
        service.update_plan(OBS, session.id, {"sample_size": 8})
    with pytest.raises(ServiceError):
        service.update_plan(LEAD, session.id, {"status": "closed"})
    with pytest.raises(ServiceError):
        service.update_plan(LEAD, session.id, {"tolerance_pct": -1})
    s = service.update_plan(LEAD, session.id, {"sample_size": 8, "sample_value_threshold": 60000}, "applied")
    assert s.sample_size == 8
    [ev] = [e for e in service.db.list_audit(session.id) if e["action"] == "plan.update"]
    assert ev["details"]["before"] == {"sample_size": 2, "sample_value_threshold": 50000.0}


# ------------------------------------------------------------------ API
def test_api_guidance_plan_and_copilot_sources(tmp_path):
    client = ScriptedChatClient([
        tool_turn("search_audit_guidance", {"query": "tag control"}),
        text_turn("Record used, unused and voided tags [G:cutoff-tags#tag-control] [G:invented#rule]."),
    ])
    app = create_app(Settings(data_dir=tmp_path), chat_client=client, knowledge=standin_source())
    c = TestClient(app)
    L, O = {"X-User": LEAD}, {"X-User": OBS}
    assert c.get("/api/me", headers=L).json()["knowledge"] == {"source": "standin", "authoritative": False}
    sid = c.post("/api/sessions", headers=L, json={"engagement": "E", "client": "C", "location": "L",
                                                    "count_date": "2026-12-31", "observers": [OBS]}).json()["id"]
    c.post(f"/api/sessions/{sid}/listing", headers=L, json={"csv": LISTING})
    hits = c.get("/api/guidance", params={"q": "third party warehouse"}, headers=O).json()["results"]
    assert hits[0]["id"] == "condition#inventory-held-for-or-by-third-parties"

    r = c.post(f"/api/sessions/{sid}/copilot", headers=O, json={"message": "what about tags?"}).json()
    assert [g["id"] for g in r["guidance_used"]] == ["cutoff-tags#tag-control"]
    assert "[citation not found]" in r["reply"] and len(r["guidance_retrieved"]) >= 1

    assert c.post(f"/api/sessions/{sid}/plan-suggestions", headers=O).status_code == 403
    client.script = [text_turn(json.dumps({"sampling": {"approach": "x", "random_sample_size": 6},
                                           "tolerance": {"units": 2}}))]
    assert c.post(f"/api/sessions/{sid}/plan-suggestions", headers=L).json()["plan"]["sampling"]["random_sample_size"] == 6
    assert c.post(f"/api/sessions/{sid}/plan", headers=O, json={"changes": {"sample_size": 6}}).status_code == 403
    assert c.post(f"/api/sessions/{sid}/plan", headers=L, json={"changes": {"sample_size": 6}}).json()["sample_size"] == 6

    off = TestClient(create_app(Settings(data_dir=tmp_path / "2"), use_env_client=False, use_env_knowledge=False))
    assert off.get("/api/guidance", params={"q": "x"}, headers=L).status_code == 503


def test_auto_uses_standin_until_the_skill_is_added(monkeypatch, tmp_path):
    from inventory_checker.knowledge import is_placeholder
    from inventory_checker.knowledge.sources import PROJECT_SKILL_DIR

    monkeypatch.delenv("AUDIT_KNOWLEDGE_SOURCE", raising=False)
    assert is_placeholder(PROJECT_SKILL_DIR)  # the repo ships the empty slot
    monkeypatch.setenv("AUDIT_KNOWLEDGE_DIR", str(PROJECT_SKILL_DIR))
    assert knowledge_from_env().name == "standin"

    skill = tmp_path / "audit-knowledge"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: audit-knowledge\nplaceholder: true\n---\n# Slot\nTo fill.\n")
    (skill / "references" / "_FORMAT.md").write_text("# Format\nnot content\n")
    (skill / "references" / "inv.md").write_text("# Test counts\n> topics: test_counts\n\nFirm rule FIRM-1.\n")
    monkeypatch.setenv("AUDIT_KNOWLEDGE_DIR", str(skill))
    assert knowledge_from_env().name == "standin"  # still marked placeholder

    (skill / "SKILL.md").write_text("---\nname: audit-knowledge\n---\n# Firm audit knowledge\nMethodology.\n")
    k = knowledge_from_env()
    assert k.name == "audit-knowledge" and k.authoritative
    assert "inv#test-counts" in {s.id for s in k.snippets}
    assert not any(s.source.endswith("_FORMAT.md") for s in k.snippets)
