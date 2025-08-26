# adapters/freshdesk.py
from typing import Dict, Any, Optional
import os
import logging
import requests

logger = logging.getLogger(__name__)

# reuse your package session if you have adapters.session
try:
    from .session import session, DEFAULT_REQUEST_TIMEOUT, DEBUG_RETURN_RAW
except Exception:
    # fallback if not present (keeps original simple shape)
    session = requests
    DEFAULT_REQUEST_TIMEOUT = int(os.getenv("ADAPTER_REQUEST_TIMEOUT_S", "30"))
    DEBUG_RETURN_RAW = False


class FreshdeskAdapter:
    def __init__(self):
        # read env at init but we'll refresh in create_ticket in case .env is loaded later
        self.domain = os.getenv("FRESHDESK_DOMAIN")
        self.api_key = os.getenv("FRESHDESK_API_KEY")

    def _validate(self) -> bool:
        return bool(self.domain and self.api_key)

    def _map_priority(self, p: Optional[str]) -> int:
        return {"low": 1, "medium": 2, "high": 3}.get((p or "").lower(), 1)

    def _refresh_config(self) -> None:
        # If .env was loaded after module import, pick up values now
        if not self.domain:
            self.domain = os.getenv("FRESHDESK_DOMAIN")
        if not self.api_key:
            self.api_key = os.getenv("FRESHDESK_API_KEY")

    def create_ticket(self, payload: dict, timeout: Optional[int] = None):
        # refresh config in case .env was loaded after adapter import
        self._refresh_config()

        if not (self.domain and self.api_key):
            return {"status": "error", "error": "Freshdesk env vars not set"}

        timeout = timeout or DEFAULT_REQUEST_TIMEOUT
        url = f"https://{self.domain}.freshdesk.com/api/v2/tickets"
        auth = (self.api_key, "X")  # Freshdesk uses API key as username and any password

        # include a default status (2 is commonly 'Open' in Freshdesk) to satisfy instances
        # that enforce status in ticket creation. You can override by passing payload["status"].
        data = {
            "subject": payload.get("subject"),
            "description": payload.get("description", ""),
            "email": (payload.get("requester") or {}).get("email"),
            "priority": self._map_priority(payload.get("priority", "low")),
            "status": payload.get("status", 2),
        }

        # remove keys with None values (Freshdesk can reject nulls)
        data = {k: v for k, v in data.items() if v is not None}

        headers = {"Content-Type": "application/json"}
        try:
            r = session().post(url, auth=auth, json=data, headers=headers, timeout=timeout)
            r.raise_for_status()
            parsed = r.json() if getattr(r, "text", None) else {}
            out = {"status": "ok"}
            if DEBUG_RETURN_RAW:
                out["raw"] = parsed
            else:
                # try to return ticket id if available
                try:
                    out["ticket_id"] = parsed.get("id") if isinstance(parsed, dict) else None
                except Exception:
                    pass
            return out
        except requests.HTTPError as e:
            # include response body and status code for easier debugging
            return {"status": "error", "code": getattr(e.response, "status_code", None), "body": getattr(e.response, "text", None), "error": str(e)}
        except Exception as e:
            logger.exception("Freshdesk API error")
            return {"status": "error", "error": str(e)}
