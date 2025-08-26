# adapters/zendesk.py
from typing import Dict, Any, Optional
import os
import logging

from . import session as session_mod

logger = logging.getLogger(__name__)


class ZendeskAdapter:
    def __init__(self):
        self.subdomain = os.getenv("ZENDESK_SUBDOMAIN")
        self.email = os.getenv("ZENDESK_EMAIL")
        self.api_token = os.getenv("ZENDESK_API_TOKEN")

    def _validate(self) -> bool:
        return bool(self.subdomain and self.email and self.api_token)

    def _map_priority(self, p: Optional[str]) -> str:
        return {"low": "low", "medium": "normal", "high": "high"}.get((p or "").lower(), "low")

    def create_ticket(self, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Create a Zendesk ticket via API. Returns {'status':'ok', 'ticket_id': ...} or error dict.
        Uses adapters.session.session() for HTTP calls so tests can monkeypatch session.
        """
        if not self._validate():
            return {"status": "error", "error": "Zendesk credentials not configured"}
        timeout = timeout or session_mod.DEFAULT_REQUEST_TIMEOUT
        url = f"https://{self.subdomain}.zendesk.com/api/v2/tickets.json"
        auth = (f"{self.email}/token", self.api_token)
        ticket = {
            "ticket": {
                "subject": payload.get("subject"),
                "comment": {"body": payload.get("description", "")},
                "priority": self._map_priority(payload.get("priority")),
            }
        }

        requester = (payload.get("requester") or {}) or {}
        if requester.get("email"):
            req = {"email": requester.get("email")}
            if requester.get("name"):
                req["name"] = requester.get("name")
            ticket["ticket"]["requester"] = req

        try:
            r = session_mod.session().post(url, auth=auth, json=ticket, timeout=timeout)
            r.raise_for_status()
            data = r.json() if getattr(r, "text", None) else {}
            tid = None
            if isinstance(data, dict):
                tid = data.get("ticket", {}).get("id")
            out = {"status": "ok", "ticket_id": tid}
            if session_mod.DEBUG_RETURN_RAW:
                out["raw"] = data
            return out
        except Exception as e:
            logger.exception("Zendesk API error")
            return {"status": "error", "error": str(e)}
