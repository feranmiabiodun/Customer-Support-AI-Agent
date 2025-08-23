#!/usr/bin/env python3
"""
run_create_ticket.py

Default: UI automation with Playwright (as required in screening doc).
Fallback: API mode if explicitly requested.

Usage:
  python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com"
  python run_create_ticket.py "Create a medium priority ticket about API 500 errors for alice@example.com" ui
  python run_create_ticket.py "Create a low priority ticket about password reset issues for alice@example.com" api
  python run_create_ticket.py "Create a low priority ticket about password reset issues for alice@example.com" ui dry-run
"""
import sys
import json
import re
from parser import parse_instruction
from adapters import zendesk_adapter, freshdesk_adapter
from agents import ui_agent

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

def redact_for_console(obj: str) -> str:
    try:
        return _EMAIL_RE.sub("[REDACTED_EMAIL]", obj)
    except Exception:
        return obj

def dispatch(parsed_json, mode="ui", dry_run: bool = False):
    """
    Dispatch ticket creation to chosen providers, either via UI automation (Playwright) or REST API.
    """
    providers = parsed_json.get("providers", ["zendesk", "freshdesk"])
    results = {}

    for p in providers:
        pname = str(p).lower()

        if mode == "api":
            if pname == "zendesk":
                results["zendesk"] = zendesk_adapter.create_ticket(parsed_json)
            elif pname == "freshdesk":
                results["freshdesk"] = freshdesk_adapter.create_ticket(parsed_json)
            else:
                results[pname] = {"status": "error", "error": "unsupported provider"}

        elif mode == "ui":
            try:
                # ui_agent shim expects (intent, provider, dry_run=...)
                results[pname] = ui_agent.create_ticket(parsed_json, pname, dry_run=dry_run)
            except Exception as e:
                results[pname] = {"status": "error", "error": str(e)}
        else:
            results[pname] = {"status": "error", "error": f"unsupported mode: {mode}"}

    return results


if __name__ == "__main__":
    # Read arguments
    if len(sys.argv) < 2:
        print("Usage: python run_create_ticket.py \"<instruction>\" [ui|api] [dry-run]")
        sys.exit(1)

    instruction = sys.argv[1]
    mode = sys.argv[2].lower() if len(sys.argv) > 2 else "ui"  # default = UI automation

    dry_run_flag = False
    if len(sys.argv) > 3:
        arg3 = sys.argv[3].lower()
        if arg3 in ("dry-run", "dryrun", "simulate", "--dry-run", "dry"):
            dry_run_flag = True

    # Parse natural language instruction into JSON
    print(f"Parsing instruction in {mode.upper()} mode (dry_run={dry_run_flag})...")
    parsed = parse_instruction(instruction)

    # redact when printing to console to avoid leaking emails
    try:
        redacted = redact_for_console(json.dumps(parsed, indent=2))
        print("Parsed instruction (redacted):")
        print(redacted)
    except Exception:
        print("Parsed instruction:")
        print(parsed)

    # Dispatch to providers
    print("\nDispatching to providers...")
    results = dispatch(parsed, mode, dry_run=dry_run_flag)
    print(json.dumps(results, indent=2))
