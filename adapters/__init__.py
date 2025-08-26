# adapters/__init__.py
from typing import Dict
from .freshdesk import FreshdeskAdapter
from .zendesk import ZendeskAdapter

# instantiate default adapters (they read env on init but do not make network calls)
freshdesk_adapter = FreshdeskAdapter()
zendesk_adapter = ZendeskAdapter()

ADAPTERS: Dict[str, object] = {
    "freshdesk": freshdesk_adapter,
    "zendesk": zendesk_adapter,
}


def register_adapter(name: str, adapter_obj) -> None:
    """Register or override an adapter by name (lowercased)."""
    ADAPTERS[name.strip().lower()] = adapter_obj


def get_adapter(name: str):
    """Return adapter instance or None."""
    if not name:
        return None
    return ADAPTERS.get(name.strip().lower())
