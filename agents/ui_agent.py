#!/usr/bin/env python3
# agents/ui_agent.py
"""
Thin shim for backwards compatibility and convenience.

- Exposes create_ticket(intent, provider, dry_run=False, **kwargs)
- Validates provider exists (either provider config or an adapter registered)
- Provides a get_agent() factory for tests to inject a shared agent instance
- Keeps small provider-specific wrappers for backwards compatibility (optional)
"""
from typing import Dict, Any, Optional
from .generic_ui_agent import GenericUIAgent
from adapters import get_adapter
import pathlib
import json
import logging

logger = logging.getLogger(__name__)

_PROVIDERS_DIR = pathlib.Path("providers")

def _provider_config_exists(provider: str) -> bool:
    if not provider:
        return False
    p = _PROVIDERS_DIR / f"{provider}.json"
    if p.exists():
        return True
    # Also allow provider names that map to adapters only
    if get_adapter(provider) is not None:
        return True
    return False

# simple factory so tests can reuse or mock agent easily
def get_agent(headless: Optional[bool] = None, slow_mo: Optional[int] = None, user_data_dir: Optional[str] = None) -> GenericUIAgent:
    return GenericUIAgent(headless=headless, slow_mo=slow_mo, user_data_dir=user_data_dir)

def create_ticket(intent: Dict[str, Any], provider: str, dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    """
    Create a ticket using UI automation for the named provider.

    Args:
      intent: canonical parsed instruction dict (subject, description, requester, priority, fields/steps optional)
      provider: provider name (e.g. 'zendesk', 'freshdesk', or other registered provider)
      dry_run: if True, return planned steps without launching the browser
      kwargs: forwarded to GenericUIAgent (headless, slow_mo, user_data_dir, timeout)

    Returns:
      dict: canonical result from GenericUIAgent.create_ticket or adapter fallback result
    """
    if not provider:
        return {"status": "error", "error": "provider is required"}

    pname = str(provider).strip().lower()
    # validate provider presence (config file or adapter)
    if not _provider_config_exists(pname):
        return {"status": "error", "error": f"unknown provider '{pname}': no providers/{pname}.json and no registered adapter"}

    agent = get_agent(headless=kwargs.get("headless"), slow_mo=kwargs.get("slow_mo"), user_data_dir=kwargs.get("user_data_dir"))
    try:
        return agent.create_ticket(pname, intent, dry_run=dry_run, timeout=kwargs.get("timeout"), headless=kwargs.get("headless"), slow_mo=kwargs.get("slow_mo"), user_data_dir=kwargs.get("user_data_dir"))
    except Exception as e:
        logger.exception("create_ticket raised an exception for provider %s", pname)
        # best-effort adapter fallback if available
        adapter = get_adapter(pname)
        if adapter:
            try:
                res = adapter.create_ticket(intent)
                return {pname: res}
            except Exception:
                logger.exception("Adapter fallback failed for provider %s", pname)
                return {"status": "error", "error": f"exception creating ticket and adapter fallback failed: {e}"}
        return {"status": "error", "error": str(e)}

# Backwards compatibility helpers (kept for callers that import them directly)
def create_ticket_zendesk(intent: Dict[str, Any], dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    return create_ticket(intent, "zendesk", dry_run=dry_run, **kwargs)

def create_ticket_freshdesk(intent: Dict[str, Any], dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    return create_ticket(intent, "freshdesk", dry_run=dry_run, **kwargs)

__all__ = ["create_ticket", "create_ticket_zendesk", "create_ticket_freshdesk", "get_agent"]
