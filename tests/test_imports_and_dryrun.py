# tests/test_imports_and_dryrun.py
import os
import json
import types
import pytest
from unittest.mock import patch

# Basic import smoke test
def test_imports():
    import parser  # noqa: F401
    from agents import generic_ui_agent  # noqa: F401
    import agents.ui_agent  # noqa: F401
    import adapters  # noqa: F401
    import llm.mock_dom_steps  # noqa: F401

# Test run_create_ticket dry-run flow by patching parser.parse_instruction
def test_run_create_ticket_dry_run(monkeypatch, tmp_path):
    import run_create_ticket
    # construct a parsed object similar to what parser.parse_instruction returns
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

    # call dispatch in UI dry-run mode
    results = run_create_ticket.dispatch(parsed, mode="ui", dry_run=True)
    assert isinstance(results, dict)
    assert "zendesk" in results
    assert results["zendesk"]["status"] == "dry-run"
    assert isinstance(results["zendesk"]["steps"], list)

# Test GenericUIAgent dry-run returns early when steps present
def test_generic_agent_dry_run(monkeypatch):
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
