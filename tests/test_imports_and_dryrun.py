import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

def test_imports():
    import parser  
    from agents import ui_agent  
    import agents.compat_ui_shim  
    import adapters  
    import llm.mock_dom_steps 

def test_run_create_ticket_dry_run(monkeypatch):
    import run_create_ticket
    parsed = {
        "action": "create_ticket",
        "subject": "test subject",
        "description": "test description",
        "requester": {"email": "tester@example.com"},
        "priority": "low",
        "providers": ["zendesk"],
        "steps": [
            {"action": "click", "selector_candidates": ["button:has-text('New')"]},
            {"action": "fill", "target": "subject", "selector_candidates": ["input[placeholder='Subject']"], "value_source": "fields.subject.value"}
        ],
        "fields": {"subject": {"value": "test subject", "selector_candidates": ["input[placeholder='Subject']"]}}
    }

    monkeypatch.setattr("parser.parse_instruction", lambda instr: parsed)
    results = run_create_ticket.dispatch(parsed, mode="ui", dry_run=True)
    assert isinstance(results, dict)
    assert "zendesk" in results
    assert results["zendesk"]["status"] == "dry-run"
    assert isinstance(results["zendesk"]["steps"], list)

def test_generic_agent_dry_run():
    from agents.ui_agent import CoreAgent
    agent = CoreAgent()
    intent = {
        "subject": "s",
        "description": "d",
        "requester": {"email": "a@b.com"},
        "steps": [{"action": "click", "selector_candidates": ["button:has-text('New')"]}],
        "fields": {}
    }
    res = agent.create_ticket("zendesk", intent, dry_run=True)
    assert res.get("status") == "dry-run"
    assert "steps" in res or "message" in res
