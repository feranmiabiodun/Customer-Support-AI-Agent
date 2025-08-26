# tests/test_adapters.py
import json
import os
import types
import pytest

# Import modules under test
import adapters
from adapters import freshdesk as freshdesk_mod
from adapters import zendesk as zendesk_mod
from adapters import session as session_mod

# --- Helpers: a tiny fake Response & Session ---
class FakeResponse:
    def __init__(self, status_code=201, json_data=None, text=None, raise_exc=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text if text is not None else json.dumps(self._json)
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json

class FakeSession:
    def __init__(self, to_return: FakeResponse):
        self._resp = to_return
        self.last_request = None

    def post(self, url, auth=None, json=None, timeout=None):
        # store the request for assertions if needed
        self.last_request = {"url": url, "auth": auth, "json": json, "timeout": timeout}
        return self._resp

# --- Tests ---

def test_freshdesk_adapter_success(monkeypatch):
    # Arrange: set environment and patch session.session() to return fake
    monkeypatch.setenv("FRESHDESK_DOMAIN", "exampledomain")
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake_key")
    fake_resp = FakeResponse(status_code=201, json_data={"id": 123, "subject": "OK"})
    fake_session = FakeSession(fake_resp)
    monkeypatch.setattr(session_mod, "session", lambda: fake_session)

    adapter = freshdesk_mod.FreshdeskAdapter()
    payload = {
        "subject": "Test subject",
        "description": "Test desc",
        "requester": {"email": "user@example.com"},
        "priority": "high"
    }

    # Act
    out = adapter.create_ticket(payload, timeout=5)

    # Assert
    assert isinstance(out, dict)
    assert out["status"] == "ok"
    # if DEBUG_RETURN_RAW env set, raw would be included; otherwise ok only
    # ensure session was called with expected shape
    assert fake_session.last_request is not None
    assert "freshdesk.com/api/v2/tickets" in fake_session.last_request["url"]
    assert fake_session.last_request["json"]["subject"] == "Test subject"
    assert fake_session.last_request["json"]["email"] == "user@example.com"

def test_zendesk_adapter_success(monkeypatch):
    # Arrange env + fake session response
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "token123")
    fake_resp = FakeResponse(status_code=201, json_data={"ticket": {"id": 42}})
    fake_session = FakeSession(fake_resp)
    monkeypatch.setattr(session_mod, "session", lambda: fake_session)

    adapter = zendesk_mod.ZendeskAdapter()
    payload = {
        "subject": "Zendesk test",
        "description": "desc",
        "requester": {"email": "enduser@example.com", "name": "End User"},
        "priority": "low"
    }

    out = adapter.create_ticket(payload, timeout=7)
    assert out["status"] == "ok"
    assert out.get("ticket_id") == 42
    # verify that the posted JSON contains the 'ticket' structure
    assert fake_session.last_request["json"]["ticket"]["subject"] == "Zendesk test"
    assert fake_session.last_request["auth"][0].endswith("/token") or isinstance(fake_session.last_request["auth"], tuple)

def test_adapters_registry_and_custom_register(monkeypatch):
    # ensure registry has keys
    assert "freshdesk" in adapters.ADAPTERS
    assert "zendesk" in adapters.ADAPTERS

    # register a fake adapter and fetch it
    class DummyAdapter:
        def create_ticket(self, payload):
            return {"ok": True, "payload": payload}

    adapters.register_adapter("dummy", DummyAdapter())
    got = adapters.get_adapter("dummy")
    assert isinstance(got, DummyAdapter)
    assert got.create_ticket({"sub":"x"})["ok"] is True
