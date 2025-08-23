# adapters/__init__.py
"""
Combined adapters module exposing simple create_ticket interfaces for Zendesk and Freshdesk.
You can split into separate modules later if desired.
"""
import os
import requests
from typing import Dict, Any

# ---------- Freshdesk adapter ----------
class _FreshdeskAdapter:
    def __init__(self):
        self.domain = os.getenv("FRESHDESK_DOMAIN")
        self.api_key = os.getenv("FRESHDESK_API_KEY")

    def map_priority_text_to_freshdesk(self, p: str):
        mapping = {"low": 1, "medium": 2, "high": 3}
        return mapping.get(p, 1)

    def create_ticket(self, payload: Dict[str, Any], timeout=30) -> Dict[str, Any]:
        if not (self.domain and self.api_key):
            return {"status": "error", "error": "Freshdesk env vars not set"}
        url = f"https://{self.domain}.freshdesk.com/api/v2/tickets"
        auth = (self.api_key, "X")
        data = {
            "subject": payload.get("subject"),
            "description": payload.get("description", ""),
            "email": payload.get("requester", {}).get("email"),
            "priority": self.map_priority_text_to_freshdesk(payload.get("priority", "low")),
            "status": 2
        }
        headers = {"Content-Type": "application/json"}
        r = requests.post(url, auth=auth, json=data, headers=headers, timeout=timeout)
        try:
            r.raise_for_status()
            return {"status": "ok", "raw": r.json()}
        except requests.HTTPError as e:
            return {"status": "error", "code": r.status_code, "body": r.text, "error": str(e)}

freshdesk_adapter = _FreshdeskAdapter()

# ---------- Zendesk adapter ----------
class _ZendeskAdapter:
    def __init__(self):
        self.subdomain = os.getenv("ZENDESK_SUBDOMAIN")
        self.email = os.getenv("ZENDESK_EMAIL")
        self.api_token = os.getenv("ZENDESK_API_TOKEN")

    def map_priority_text_to_zendesk(self, p: str):
        mapping = {"low": "low", "medium": "normal", "high": "high"}
        return mapping.get(p, "low")

    def create_ticket(self, payload: Dict[str, Any], timeout=30) -> Dict[str, Any]:
        if not (self.subdomain and self.email and self.api_token):
            return {"status": "error", "error": "Zendesk env vars not set"}
        url = f"https://{self.subdomain}.zendesk.com/api/v2/tickets.json"
        auth = (f"{self.email}/token", self.api_token)
        ticket = {
            "ticket": {
                "subject": payload.get("subject"),
                "comment": {"body": payload.get("description", "")},
                "priority": self.map_priority_text_to_zendesk(payload.get("priority", "low")),
            }
        }
        requester = payload.get("requester", {})
        if requester.get("email"):
            ticket["ticket"]["requester"] = {
                "name": requester.get("name"),
                "email": requester.get("email")
            }
        headers = {"Content-Type": "application/json"}
        r = requests.post(url, json=ticket, auth=auth, headers=headers, timeout=timeout)
        try:
            r.raise_for_status()
            data = r.json()
            tid = data.get("ticket", {}).get("id")
            return {"status": "ok", "ticket_id": tid, "raw": data}
        except requests.HTTPError as e:
            return {"status": "error", "code": r.status_code, "body": r.text, "error": str(e)}

zendesk_adapter = _ZendeskAdapter()

__all__ = ["freshdesk_adapter", "zendesk_adapter"]
