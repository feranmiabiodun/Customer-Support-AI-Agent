# adapters/__init__.py
"""
Combined adapters module exposing simple create_ticket interfaces for Zendesk and Freshdesk.
You can split into separate modules later if desired.
"""

# adapters/__init__.py
"""
Adapters package entrypoint & simple registry.

- Provides robust, testable adapters for Zendesk and Freshdesk.
- Uses a shared requests.Session with retry/backoff (if urllib3 Retry is available).
- Exposes register_adapter(provider_name, adapter_instance) and get_adapter(provider_name).
- Adapters implement .create_ticket(payload: Dict[str, Any]) -> Dict[str, Any]
  and return a normalized response:
    {"status": "ok", "ticket_id": <id_or_none>, "raw": <api_json_if_debug>}
  or:
    {"status": "error", "error": "...", "code": <http_status?>, "body": "<response body?>"}

To add a new provider:
- Create adapters/<provider>.py with a class implementing the adapter interface.
- Import and register it in this module (or call register_adapter from the provider module).
"""

from __future__ import annotations
import os
import logging
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Shared session with optional retry/backoff
_SESSION = requests.Session()
DEFAULT_REQUEST_TIMEOUT = int(os.getenv("ADAPTER_REQUEST_TIMEOUT_S", "30"))
_DEBUG_RETURN_RAW = os.getenv("DEBUG_RETURN_RAW_API_RESPONSES", "false").strip().lower() in ("1", "true", "yes")

# Install a Retry strategy if urllib3 is available (requests >= common installations)
try:
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter

    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"]),
        backoff_factor=0.5,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    _SESSION.mount("https://", adapter)
    _SESSION.mount("http://", adapter)
except Exception:
    # If urllib3 isn't available for some reason, continue without advanced retries.
    logger.debug("urllib3 Retry not available; proceeding without advanced retry policy")


class BaseAdapter:
    """Minimal adapter interface / helper methods."""

    def create_ticket(self, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Implement in subclass. Return normalized dict with 'status' key.
        """
        raise NotImplementedError


# ------------------ Freshdesk adapter ------------------
class FreshdeskAdapter(BaseAdapter):
    def __init__(self):
        self.domain = os.getenv("FRESHDESK_DOMAIN")
        self.api_key = os.getenv("FRESHDESK_API_KEY")
        self._validate_config()

    def _validate_config(self):
        if not (self.domain and self.api_key):
            logger.debug("FreshdeskAdapter: domain or api_key not configured")

    def map_priority(self, p: str) -> int:
        mapping = {"low": 1, "medium": 2, "high": 3}
        return mapping.get((p or "").strip().lower(), 1)

    def create_ticket(self, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        if not (self.domain and self.api_key):
            return {"status": "error", "error": "Freshdesk credentials not configured (FRESHDESK_DOMAIN/FRESHDESK_API_KEY missing)"}
        timeout = timeout or DEFAULT_REQUEST_TIMEOUT
        url = f"https://{self.domain}.freshdesk.com/api/v2/tickets"
        auth = (self.api_key, "X")
        data = {
            "subject": payload.get("subject"),
            "description": payload.get("description", ""),
            "email": (payload.get("requester") or {}).get("email"),
            "priority": self.map_priority(payload.get("priority", "low")),
            "status": payload.get("status", 2)
        }
        headers = {"Content-Type": "application/json"}
        try:
            r = _SESSION.post(url, auth=auth, json=data, headers=headers, timeout=timeout)
            r.raise_for_status()
            parsed = r.json() if r.text else {}
            out = {"status": "ok"}
            if _DEBUG_RETURN_RAW:
                out.update({"raw": parsed})
            return out
        except requests.HTTPError as e:
            resp = getattr(e.response, "text", None)
            code = getattr(e.response, "status_code", None)
            logger.debug("Freshdesk API error: %s %s", code, resp)
            return {"status": "error", "error": str(e), "code": code, "body": resp}
        except Exception as e:
            logger.exception("Freshdesk request failed")
            return {"status": "error", "error": str(e)}


# ------------------ Zendesk adapter ------------------
class ZendeskAdapter(BaseAdapter):
    def __init__(self):
        self.subdomain = os.getenv("ZENDESK_SUBDOMAIN")
        self.email = os.getenv("ZENDESK_EMAIL")
        self.api_token = os.getenv("ZENDESK_API_TOKEN")
        self._validate_config()

    def _validate_config(self):
        if not (self.subdomain and self.email and self.api_token):
            logger.debug("ZendeskAdapter: credentials not fully configured (ZENDESK_SUBDOMAIN/ZENDESK_EMAIL/ZENDESK_API_TOKEN)")

    def map_priority(self, p: str) -> str:
        mapping = {"low": "low", "medium": "normal", "high": "high"}
        return mapping.get((p or "").strip().lower(), "low")

    def create_ticket(self, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        if not (self.subdomain and self.email and self.api_token):
            return {"status": "error", "error": "Zendesk credentials not configured (ZENDESK_SUBDOMAIN/ZENDESK_EMAIL/ZENDESK_API_TOKEN missing)"}
        timeout = timeout or DEFAULT_REQUEST_TIMEOUT
        url = f"https://{self.subdomain}.zendesk.com/api/v2/tickets.json"
        auth = (f"{self.email}/token", self.api_token)

        ticket = {
            "ticket": {
                "subject": payload.get("subject"),
                "comment": {"body": payload.get("description", "")},
                "priority": self.map_priority(payload.get("priority", "low")),
            }
        }

        requester = (payload.get("requester") or {}) or {}
        r_email = requester.get("email")
        r_name = (requester.get("name") or "").strip() if isinstance(requester.get("name"), str) else None
        if r_email:
            req = {"email": r_email}
            if r_name:
                req["name"] = r_name
            ticket["ticket"]["requester"] = req

        headers = {"Content-Type": "application/json"}
        try:
            r = _SESSION.post(url, json=ticket, auth=auth, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json() if r.text else {}
            ticket_id = data.get("ticket", {}).get("id") if isinstance(data, dict) else None
            out = {"status": "ok", "ticket_id": ticket_id}
            if _DEBUG_RETURN_RAW:
                out["raw"] = data
            return out
        except requests.HTTPError as e:
            resp = getattr(e.response, "text", None)
            code = getattr(e.response, "status_code", None)
            logger.debug("Zendesk API error: %s %s", code, resp)
            return {"status": "error", "error": str(e), "code": code, "body": resp}
        except Exception as e:
            logger.exception("Zendesk request failed")
            return {"status": "error", "error": str(e)}


# instantiate adapters (can be replaced/overridden in tests)
freshdesk_adapter = FreshdeskAdapter()
zendesk_adapter = ZendeskAdapter()

# registry
ADAPTERS: Dict[str, BaseAdapter] = {
    "freshdesk": freshdesk_adapter,
    "zendesk": zendesk_adapter,
}


def register_adapter(provider_name: str, adapter_obj: BaseAdapter) -> None:
    if not provider_name or not adapter_obj:
        return
    ADAPTERS[provider_name.strip().lower()] = adapter_obj


def get_adapter(provider_name: str) -> Optional[BaseAdapter]:
    if not provider_name:
        return None
    return ADAPTERS.get(provider_name.strip().lower())


__all__ = ["freshdesk_adapter", "zendesk_adapter", "ADAPTERS", "get_adapter", "register_adapter", "BaseAdapter"]
