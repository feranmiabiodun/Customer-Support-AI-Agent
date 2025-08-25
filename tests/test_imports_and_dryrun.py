import pytest

def test_imports():
    import parser  # noqa: F401
    from agents import generic_ui_agent  # noqa: F401
    import agents.ui_agent  # noqa: F401
    import adapters  # noqa: F401
    import llm.mock_dom_steps  # noqa: F401

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
    from agents.generic_ui_agent import GenericUIAgent
    agent = GenericUIAgent()
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
