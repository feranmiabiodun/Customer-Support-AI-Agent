# llm/mock_dom_steps.py
"""
Generate deterministic, mock DOM 'steps' for a parsed ticket object.
This is intentionally simple: it attaches candidate selectors we already
use heuristically in ui_agent so the UI agent can execute 'LLM-produced' steps.
"""

from typing import Dict, Any, List

_SUBJECT_SELECTORS = [
    "input[placeholder='Subject']",
    "input[placeholder*='Subject']",
    "input[id*='subject']",
    "textarea[placeholder='Subject']",
    "div:has-text('Subject') input",
]

_DESCRIPTION_SELECTORS = [
    "textarea[name='comment']",
    "textarea[name='description']",
    "textarea[id*='comment']",
    "div[role='textbox'][contenteditable='true']",
    "div[contenteditable='true']"
]

_REQUESTER_SELECTORS = [
    "input[placeholder*='Search or add requester']",
    "input[aria-label*='requester']",
    "input[placeholder*='Requester']",
    "input[name*='requester']",
    "div[role='combobox'] input"
]

_SUBMIT_SELECTORS = [
    "button:has-text('Submit as New')",
    "button:has-text('Submit')",
    "button:has-text('Save')",
    "button:has-text('Create')",
    "button:has-text('New')",
]

def _field_block(name: str, value: str, selectors: List[str]) -> Dict[str, Any]:
    return {"value": value or "", "field_name": name, "selector_candidates": selectors}

def generate_dom_steps(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns a dict containing 'fields' and 'steps' describing DOM actions.
    This is a mock deterministic mapping (not a real DOM scanner).
    - fields: mapping of logical field name -> {value, selector_candidates}
    - steps: ordered list of actions (click/fill) referencing selector_candidates
    """
    fields = {}
    # subject
    fields["subject"] = _field_block("subject", parsed.get("subject", ""), _SUBJECT_SELECTORS)
    # description
    descr = parsed.get("description", "") or parsed.get("comment", "") or ""
    fields["description"] = _field_block("description", descr, _DESCRIPTION_SELECTORS)
    # requester
    requester_email = ""
    if isinstance(parsed.get("requester"), dict):
        requester_email = parsed["requester"].get("email", "")
        requester_name = parsed["requester"].get("name", "")
    else:
        requester_email = parsed.get("requester", "") or ""
        requester_name = ""
    fields["requester"] = {
        "value": requester_email,
        "name": requester_name,
        "field_name": "requester",
        "selector_candidates": _REQUESTER_SELECTORS
    }

    steps: List[Dict[str, Any]] = []
    # click "New" / "Add" (open new ticket)
    steps.append({"action": "click", "selector_candidates": ["button:has-text('Add')", "button:has-text('+ New ticket')", "button:has-text('New')", "a[href*='/agent/tickets/new']", "button[aria-label='New']"], "explain": "open new ticket panel"})
    # fill requester
    steps.append({"action": "fill", "target": "requester", "selector_candidates": _REQUESTER_SELECTORS, "value_source": "fields.requester.value"})
    # fill subject
    steps.append({"action": "fill", "target": "subject", "selector_candidates": _SUBJECT_SELECTORS, "value_source": "fields.subject.value"})
    # fill description
    steps.append({"action": "fill", "target": "description", "selector_candidates": _DESCRIPTION_SELECTORS, "value_source": "fields.description.value"})
    # final submit
    steps.append({"action": "click", "selector_candidates": _SUBMIT_SELECTORS, "explain": "submit ticket"})

    return {"fields": fields, "steps": steps}
