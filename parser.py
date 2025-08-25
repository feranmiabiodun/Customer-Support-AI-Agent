#!/usr/bin/env python3
"""
parser.py

Enhancements:
- USE_MOCK_DOM short-circuits LLM calls and produces a minimal parsed object,
  letting llm/mock_dom_steps attach steps deterministically.
- Improved extraction and small repair heuristics for LLM output.
- Provides a robust final canonical normalization.
- Includes an optional few-shot example section (when GROQ_FEW_SHOT env var set).
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

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

PROMPT_PATH = "prompt_template.txt"
SCHEMA_PATH = "schema.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
DEFAULT_GROQ_RETRIES = 2
DEFAULT_GROQ_BACKOFF = 1.0  # seconds


def load_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def render_prompt(user_instruction: str) -> str:
    template = load_file(PROMPT_PATH)
    prompt = template.replace("<<USER_INSTRUCTION>>", user_instruction.strip())
    if os.getenv("GROQ_FEW_SHOT", "").strip().lower() in ("1", "true", "yes"):
        # Very small example appended to encourage JSON-only behavior (non-sensitive)
        example = '\n\nExample:\n{"action":"create_ticket","subject":"Example","description":"desc","requester":{"email":"ex@example.com"},"priority":"low","metadata":{}}\n'
        prompt += example
    return prompt


def call_groq(prompt_text: str, timeout: int = 30, retries: int = DEFAULT_GROQ_RETRIES, backoff: float = DEFAULT_GROQ_BACKOFF) -> Dict[str, Any]:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set in environment")

    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt_text}], "temperature": 0}
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


def strip_code_fences(s: str) -> str:
    s = re.sub(r"^```(?:json)?\s*", "", s.strip(), flags=re.I | re.M)
    s = re.sub(r"\s*```$", "", s, flags=re.I | re.M)
    return s.strip()


def extract_json_from_response(resp_json: Dict[str, Any]) -> Any:
    candidates: List[str] = []
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

    if "text" in resp_json and isinstance(resp_json["text"], str):
        candidates.append(resp_json["text"])
    if "message" in resp_json and isinstance(resp_json["message"], dict) and "content" in resp_json["message"]:
        candidates.append(resp_json["message"]["content"])

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
        # find first JSON block
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
        try:
            return json.loads(s_candidate)
        except json.JSONDecodeError:
            # attempt to fix common issues
            fixed = s_candidate.replace("'", '"')
            fixed = re.sub(r",\s*}", "}", fixed)
            fixed = re.sub(r",\s*\]", "]", fixed)
            try:
                return json.loads(fixed)
            except Exception:
                logger.debug("Failed to parse candidate after fixes; continuing", exc_info=True)
                continue
    raise ValueError("Could not extract valid JSON from LLM response")


def validate_against_schema(obj: Dict[str, Any]) -> None:
    schema_text = load_file(SCHEMA_PATH)
    schema = json.loads(schema_text)
    validate(instance=obj, schema=schema)


def pretty_validation_error(e: ValidationError, instance_obj: Any = None) -> str:
    parts = [f"ValidationError: {e.message}"]
    if e.path:
        parts.append(f"  path: {list(e.path)}")
    if e.schema_path:
        parts.append(f"  schema_path: {list(e.schema_path)}")
    if instance_obj is not None:
        try:
            parts.append(f"  instance snapshot (truncated): {json.dumps(instance_obj)[:800]}")
        except Exception:
            parts.append(f"  instance snapshot (repr): {repr(instance_obj)[:800]}")
    return "\n".join(parts)


def coerce_requester(value: Any) -> Dict[str, str]:
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
    p = dict(parsed)
    if "action" not in p:
        logger.info("'action' missing from parsed output — coercing to 'create_ticket'")
        p["action"] = "create_ticket"
    if "requester" in p:
        try:
            p["requester"] = coerce_requester(p["requester"])
        except Exception as e:
            raise ValueError(f"Failed to normalize 'requester': {e}")
    if "priority" in p and isinstance(p["priority"], str):
        p["priority"] = p["priority"].strip().lower()
    if "providers" in p:
        p["providers"] = _canonicalize_providers(p["providers"])
    return p


def _final_runner_normalize(parsed: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(parsed)
    if not p.get("providers"):
        p["providers"] = ["zendesk", "freshdesk"]
    else:
        p["providers"] = _canonicalize_providers(p["providers"])
        if not p["providers"]:
            p["providers"] = ["zendesk", "freshdesk"]
    if "requester" in p and isinstance(p["requester"], str):
        p["requester"] = coerce_requester(p["requester"])
    if "priority" in p and isinstance(p["priority"], str):
        p["priority"] = p["priority"].strip().lower()
    if not p.get("description") and p.get("comment"):
        p["description"] = p.pop("comment")
    if not p.get("description") or (isinstance(p.get("description"), str) and p["description"].strip() == ""):
        subject = p.get("subject", "").strip() or "<no subject provided>"
        requester_email = "<unknown requester>"
        try:
            requester_email = p["requester"].get("email", requester_email) if isinstance(p.get("requester"), dict) else requester_email
        except Exception:
            requester_email = "<unknown requester>"
        p["description"] = f"{subject} — reported by {requester_email}. (Auto-generated: original instruction omitted a description.)"
        logger.warning("LLM output missing 'description' — auto-filled fallback description.")
    if not p.get("subject"):
        raise ValueError("Parsed instruction missing required 'subject' field")
    if "requester" not in p:
        raise ValueError("Parsed instruction missing required 'requester' field")
    if not isinstance(p["requester"], dict) or not p["requester"].get("email"):
        raise ValueError("Parsed instruction 'requester' must be a dict containing 'email'")
    return p


def parse_instruction(user_instruction: str) -> Dict[str, Any]:
    try:
        use_mock_dom = os.getenv("USE_MOCK_DOM", "true").strip().lower() not in ("0", "false", "no")
    except Exception:
        use_mock_dom = True

    prompt_text = render_prompt(user_instruction)
    parsed_raw: Dict[str, Any]

    if use_mock_dom:
        m = _EMAIL_RE.search(user_instruction)
        requester = {"email": m.group(1)} if m else {"email": "unknown@example.com"}
        parsed_raw = {
            "action": "create_ticket",
            "subject": user_instruction.strip()[:200],
            "description": user_instruction.strip(),
            "requester": requester,
            "priority": "low",
            "providers": ["zendesk", "freshdesk"]
        }
        logger.info("Mock mode active: skipping LLM call and using minimal parsed object.")
    else:
        resp = call_groq(prompt_text)
        parsed_raw = extract_json_from_response(resp)

    logger.info("Parsed object (before validation):")
    try:
        logger.info(json.dumps(parsed_raw, indent=2))
    except Exception:
        logger.info(repr(parsed_raw))

    if not isinstance(parsed_raw, dict):
        if isinstance(parsed_raw, list) and len(parsed_raw) > 0 and isinstance(parsed_raw[0], dict):
            logger.info("Parsed JSON is a list; using first element as the instruction object.")
            parsed_raw = parsed_raw[0]
        else:
            raise ValueError("Parsed JSON is not an object (dict). Schema expects a JSON object.")

    try:
        validate_against_schema(parsed_raw)
        validated = parsed_raw
    except ValidationError as e:
        logger.warning("Initial validation failed. Details:\n%s", pretty_validation_error(e, instance_obj=parsed_raw))
        try:
            normalized = normalize_parsed(parsed_raw)
            validate_against_schema(normalized)
            validated = normalized
        except ValidationError as e2:
            err_msg = pretty_validation_error(e2, instance_obj=normalized if 'normalized' in locals() else parsed_raw)
            raise ValueError("Validation failed even after normalization:\n" + err_msg)
        except Exception as e3:
            raise ValueError(f"Normalization failed: {e3}")

    final = _final_runner_normalize(validated)
    final.setdefault("meta", {})
    try:
        final["meta"]["instruction"] = user_instruction
    except Exception:
        pass

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

    logger.debug("Final canonical parsed object ready for dispatch:\n%s", json.dumps(final, indent=2))
    return final


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
