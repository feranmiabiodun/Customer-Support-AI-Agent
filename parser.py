#!/usr/bin/env python3
"""
parser.py 

- Reads prompt_template.txt and schema.json
- Calls Groq API and extracts JSON output
- Validates against schema
- Adds tolerant normalization for common LLM shorthand (action missing, requester as string, etc.)
- Provides final canonical normalization expected by run_create_ticket.py
"""
from __future__ import annotations
import os
import json
import re 
import time
import logging
from typing import Any, Dict, List

import requests
from requests.exceptions import RequestException
from jsonschema import validate, ValidationError
from dotenv import load_dotenv

load_dotenv()

# --- Configuration / env ----------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

PROMPT_PATH = "prompt_template.txt"
SCHEMA_PATH = "schema.json"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Internal helpers
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
DEFAULT_GROQ_RETRIES = 2
DEFAULT_GROQ_BACKOFF = 1.0  # seconds


# -------------------- File / prompt helpers ---------------------------------
def load_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def render_prompt(user_instruction: str) -> str:
    template = load_file(PROMPT_PATH)
    return template.replace("<<USER_INSTRUCTION>>", user_instruction.strip())


# -------------------- Groq / LLM call with retries --------------------------
def call_groq(prompt_text: str, timeout: int = 30, retries: int = DEFAULT_GROQ_RETRIES, backoff: float = DEFAULT_GROQ_BACKOFF) -> Dict[str, Any]:
    """
    Call the Groq/OpenAI-compatible endpoint and return parsed JSON response.
    Retries a few times on network errors.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set in environment")

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    last_exc = None
    for attempt in range(retries + 1):
        try:
            logger.debug("POST %s model=%s (attempt %d)", GROQ_URL, GROQ_MODEL, attempt + 1)
            r = requests.post(GROQ_URL, json=payload, headers=headers, timeout=timeout)
            if not r.ok:
                text_preview = r.text[:1000]
                raise RuntimeError(f"Groq API error: HTTP {r.status_code}: {text_preview}")
            return r.json()
        except RequestException as rexc:
            last_exc = rexc
            logger.warning("Groq RequestException (attempt %d): %s — retrying in %.1fs", attempt + 1, rexc, backoff)
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError(f"Failed to call Groq after {retries + 1} attempts: {last_exc}")


# -------------------- JSON extraction utilities -----------------------------
def strip_code_fences(s: str) -> str:
    """Remove ```json ... ``` or ```...``` fences and trim whitespace."""
    s = re.sub(r"^```(?:json)?\s*", "", s.strip(), flags=re.I | re.M)
    s = re.sub(r"\s*```$", "", s, flags=re.I | re.M)
    return s.strip()


def extract_json_from_response(resp_json: Dict[str, Any]) -> Any:
    """
    Extract JSON string from Groq/OpenAI-like response and parse it.
    Returns Python object (dict/list/etc). Raises ValueError if none parseable.
    """
    candidates: List[str] = []

    # Typical OpenAI-style response: resp_json["choices"][0]["message"]["content"]
    try:
        choices = resp_json.get("choices", [])
        if choices and isinstance(choices, list):
            for c in choices:
                if isinstance(c, dict):
                    if "message" in c and isinstance(c["message"], dict) and "content" in c["message"]:
                        candidates.append(c["message"]["content"])
                    if "text" in c and isinstance(c["text"], str):
                        candidates.append(c["text"])
    except Exception:
        logger.debug("Exception while harvesting choices", exc_info=True)

    # fallback: top-level fields
    if "text" in resp_json and isinstance(resp_json["text"], str):
        candidates.append(resp_json["text"])
    if "message" in resp_json and isinstance(resp_json["message"], dict) and "content" in resp_json["message"]:
        candidates.append(resp_json["message"]["content"])

    # lastly: the whole response as a string (last-resort)
    try:
        candidates.append(json.dumps(resp_json))
    except Exception:
        candidates.append(str(resp_json))

    logger.debug("Extracted response candidates (trimmed):")
    for i, c in enumerate(candidates):
        if not c:
            continue
        preview = c if len(c) <= 400 else (c[:400] + " ...[truncated]")
        logger.debug("  candidate[%d]: %s", i, preview)

    for c in candidates:
        if not c:
            continue
        s = strip_code_fences(c)

        # find the first JSON object/array in the candidate
        m_obj = re.search(r"(\{[\s\S]*\})", s)
        m_arr = re.search(r"(\[[\s\S]*\])", s)

        if m_obj and m_arr:
            obj_text = m_obj.group(1)
            arr_text = m_arr.group(1)
            s_candidate = obj_text if len(obj_text) >= len(arr_text) else arr_text
        elif m_obj:
            s_candidate = m_obj.group(1)
        elif m_arr:
            s_candidate = m_arr.group(1)
        else:
            s_candidate = s

        preview_choice = s_candidate if len(s_candidate) <= 400 else (s_candidate[:400] + " ...[truncated]")
        logger.debug("Trying to parse candidate (trimmed): %s", preview_choice)

        # Try normal JSON parse
        try:
            return json.loads(s_candidate)
        except json.JSONDecodeError:
            # Attempt small fixes (single->double quotes) as a best-effort
            try:
                fixed = s_candidate.replace("'", '"')
                return json.loads(fixed)
            except Exception:
                logger.debug("Failed to parse candidate after fixes; continuing", exc_info=True)
                continue

    raise ValueError("Could not extract valid JSON from LLM response")


# -------------------- Schema validation ------------------------------------
def validate_against_schema(obj: Dict[str, Any]) -> None:
    schema_text = load_file(SCHEMA_PATH)
    schema = json.loads(schema_text)
    validate(instance=obj, schema=schema)


def pretty_validation_error(e: ValidationError, instance_obj: Any = None) -> str:
    parts = [f"ValidationError: {e.message}"]
    if e.path:
        parts.append(f"  path (where in instance): {list(e.path)}")
    if e.schema_path:
        parts.append(f"  schema_path (which rule failed): {list(e.schema_path)}")
    if instance_obj is not None:
        try:
            parts.append(f"  instance snapshot (truncated): {json.dumps(instance_obj)[:800]}")
        except Exception:
            parts.append(f"  instance snapshot (repr): {repr(instance_obj)[:800]}")
    return "\n".join(parts)


# -------------------- Normalization helpers --------------------------------
def coerce_requester(value: Any) -> Dict[str, str]:
    """
    Accepts either:
      - dict with 'email' (ideal)
      - string like 'bob@example.com' or 'Bob <bob@example.com>'
    Returns requester dict with at least 'email' key.
    """
    if isinstance(value, dict):
        if "email" in value and value["email"]:
            return value
        for v in value.values():
            if isinstance(v, str):
                m = _EMAIL_RE.search(v)
                if m:
                    value["email"] = m.group(1)
                    return value
        raise ValueError("requester object missing 'email' field")

    if isinstance(value, str):
        m = _EMAIL_RE.search(value)
        if not m:
            raise ValueError(f"Could not parse email from requester string: {value!r}")
        email = m.group(1)
        name_part = value.split(email)[0].strip()
        name_part = re.sub(r'[<>"\']', '', name_part).strip()
        requester = {"email": email}
        if name_part:
            requester["name"] = name_part
        return requester

    raise ValueError("Unsupported requester type; expected dict or string")


def _canonicalize_providers(providers) -> List[str]:
    """
    Ensure providers becomes a list of lowercase provider names.
    Accepts: "zendesk", ["zendesk","freshdesk"], [{"name":"zendesk"}, ...], "zendesk,freshdesk"
    """
    if not providers:
        return []
    if isinstance(providers, str):
        return [p.strip().lower() for p in providers.split(",") if p.strip()]
    if isinstance(providers, dict) and "name" in providers:
        return [str(providers["name"]).strip().lower()]
    if isinstance(providers, list):
        out: List[str] = []
        for x in providers:
            if isinstance(x, str):
                out.append(x.strip().lower())
            elif isinstance(x, dict) and "name" in x:
                out.append(str(x["name"]).strip().lower())
            else:
                out.append(str(x).strip().lower())
        return out
    return [str(providers).strip().lower()]


def normalize_parsed(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply tolerant normalization for common LLM shorthand before re-validating.
    Returns a shallow-copied normalized dict.
    """
    p = dict(parsed)  # shallow copy

    # 1) Ensure 'action' exists
    if "action" not in p:
        logger.info("'action' missing from parsed output — coercing to 'create_ticket'")
        p["action"] = "create_ticket"

    # 2) Normalize requester (string -> dict with email)
    if "requester" in p:
        try:
            p["requester"] = coerce_requester(p["requester"])
        except Exception as e:
            raise ValueError(f"Failed to normalize 'requester': {e}")

    # 3) Normalize priority (string -> lowercase enum expected by schema)
    if "priority" in p and isinstance(p["priority"], str):
        p["priority"] = p["priority"].strip().lower()

    # 4) Normalize providers shape (do not force defaults here; final normalizer will)
    if "providers" in p:
        p["providers"] = _canonicalize_providers(p["providers"])

    return p


def _final_runner_normalize(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Final canonical shape expected by the runner/dispatcher.
    Ensures:
      - providers is a non-empty list (defaults to zendesk + freshdesk)
      - requester is a dict with email
      - description field exists (normalizes comment -> description) *or* auto-fills it
      - subject exists
    Raises ValueError on missing required fields (subject, requester.email).
    """
    p = dict(parsed)

    # providers default
    if not p.get("providers"):
        p["providers"] = ["zendesk", "freshdesk"]
    else:
        p["providers"] = _canonicalize_providers(p["providers"])
        if not p["providers"]:
            p["providers"] = ["zendesk", "freshdesk"]

    # requester ensure dict
    if "requester" in p and isinstance(p["requester"], str):
        p["requester"] = coerce_requester(p["requester"])

    # priority canonicalization
    if "priority" in p and isinstance(p["priority"], str):
        p["priority"] = p["priority"].strip().lower()

    # description vs comment normalization
    if not p.get("description") and p.get("comment"):
        p["description"] = p.pop("comment")

    # If description is still empty/blank, create a helpful default from subject/requester
    if not p.get("description") or (isinstance(p.get("description"), str) and p["description"].strip() == ""):
        subject = p.get("subject", "").strip() or "<no subject provided>"
        requester_email = "<unknown requester>"
        try:
            requester_email = p["requester"].get("email", requester_email) if isinstance(p.get("requester"), dict) else requester_email
        except Exception:
            requester_email = "<unknown requester>"
        p["description"] = f"{subject} — reported by {requester_email}. (Auto-generated: original instruction omitted a description.)"
        logger.warning("LLM output missing 'description' — auto-filled fallback description.")

    # required fields: subject, requester.email
    if not p.get("subject"):
        raise ValueError("Parsed instruction missing required 'subject' field")
    if "requester" not in p:
        raise ValueError("Parsed instruction missing required 'requester' field")
    if not isinstance(p["requester"], dict) or not p["requester"].get("email"):
        raise ValueError("Parsed instruction 'requester' must be a dict containing 'email'")

    return p

# -------------------- Main parse flow --------------------------------------
def parse_instruction(user_instruction: str) -> Dict[str, Any]:
    """
    Main entrypoint: render prompt, call Groq, extract JSON, validate, normalize,
    and return a final canonical dict ready for the run_create_ticket dispatcher.
    """
    prompt_text = render_prompt(user_instruction)
    resp = call_groq(prompt_text)

    # extract JSON from LLM response
    parsed_raw = extract_json_from_response(resp)

    logger.info("Parsed object from LLM (before validation):")
    try:
        logger.info(json.dumps(parsed_raw, indent=2))
    except Exception:
        logger.info(repr(parsed_raw))

    # If it's not a dict but a list with dicts, use first element
    if not isinstance(parsed_raw, dict):
        if isinstance(parsed_raw, list) and len(parsed_raw) > 0 and isinstance(parsed_raw[0], dict):
            logger.info("Parsed JSON is a list; using first element as the instruction object.")
            parsed_raw = parsed_raw[0]
        else:
            raise ValueError("Parsed JSON is not an object (dict). Schema expects a JSON object.")

    # 1) Try validate as-is
    try:
        validate_against_schema(parsed_raw)
        logger.info("Parsed object validated against schema (no normalization needed).")
        validated = parsed_raw
    except ValidationError as e:
        logger.warning("Initial validation failed. Details:\n%s", pretty_validation_error(e, instance_obj=parsed_raw))
        # 2) Attempt normalization and re-validate
        try:
            normalized = normalize_parsed(parsed_raw)
            logger.debug("Normalized parsed object (attempting re-validation):\n%s", json.dumps(normalized, indent=2))
            validate_against_schema(normalized)
            logger.info("Normalized object validated successfully.")
            validated = normalized
        except ValidationError as e2:
            err_msg = pretty_validation_error(e2, instance_obj=normalized if 'normalized' in locals() else parsed_raw)
            raise ValueError("Validation failed even after normalization:\n" + err_msg)
        except Exception as e3:
            raise ValueError(f"Normalization failed: {e3}")

    # 3) Final runner-oriented normalization (canonical providers, required fields, etc.)
    final = _final_runner_normalize(validated)

    # Attach small meta with original instruction so downstream agents can use it for LLM prompts.
    final.setdefault("meta", {})
    try:
        final["meta"]["instruction"] = user_instruction
    except Exception:
        pass

    # ------------------ Optional: attach mock LLM->DOM steps (non-fatal) --------------------
    try:
        use_mock_dom = os.getenv("USE_MOCK_DOM", "true").strip().lower() not in ("0", "false", "no")
    except Exception:
        use_mock_dom = True

    if use_mock_dom:
        try:
            from llm.mock_dom_steps import generate_dom_steps  # type: ignore
            try:
                dom_bundle = generate_dom_steps(final)
                if dom_bundle:
                    final["steps"] = dom_bundle.get("steps", [])
                    final.setdefault("fields", {}).update(dom_bundle.get("fields", {}))
                    logger.info("Mock DOM steps attached to parsed object (MOCK).")
            except Exception:
                logger.debug("generate_dom_steps failed; continuing without DOM steps", exc_info=True)
        except Exception:
            logger.debug("No mock_dom_steps module available; skipping DOM steps attachment")
    # ----------------------------------------------------------------------------------------

    logger.debug("Final canonical parsed object ready for dispatch:\n%s", json.dumps(final, indent=2))
    return final


# -------------------- CLI for manual testing --------------------------------
if __name__ == "__main__":
    import sys

    instr = " ".join(sys.argv[1:]) or "Create a low priority ticket about login failures for alice@example.com"
    try:
        result = parse_instruction(instr)
        print("\nFinal parsed instruction:")
        print(json.dumps(result, indent=2))
    except Exception as e:
        logger.exception("ERROR parsing instruction: %s", e)
        raise
