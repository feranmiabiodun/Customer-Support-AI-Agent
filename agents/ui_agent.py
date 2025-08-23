#!/usr/bin/env python3
# agents/ui_agent.py
"""
Thin shim for backwards compatibility: instantiates GenericUIAgent and calls create_ticket.
Keeps the same external functions other scripts expect.
"""
from typing import Dict, Any
from .generic_ui_agent import GenericUIAgent

def create_ticket(intent: Dict[str, Any], provider: str, dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    agent = GenericUIAgent(headless=kwargs.get("headless"), slow_mo=kwargs.get("slow_mo"), user_data_dir=kwargs.get("user_data_dir"))
    return agent.create_ticket(provider, intent, dry_run=dry_run, headless=kwargs.get("headless"), slow_mo=kwargs.get("slow_mo"), timeout=kwargs.get("timeout"))

def create_ticket_zendesk(intent: Dict[str, Any], dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    return create_ticket(intent, "zendesk", dry_run=dry_run, **kwargs)

def create_ticket_freshdesk(intent: Dict[str, Any], dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    return create_ticket(intent, "freshdesk", dry_run=dry_run, **kwargs)
