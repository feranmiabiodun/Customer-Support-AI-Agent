#!/usr/bin/env python3
# agents/generic_ui_agent.py
"""
GenericUIAgent (config-driven).

This agent:
- Loads provider configs from `providers/*.json` if present (falls back to inline defaults).
- Attempts to use LLM (via parser.call_groq + parser.extract_json_from_response) to infer DOM steps
  from a live page when `intent["steps"]` is missing.
- Executes DOM steps robustly using safe_fill/safe_click, with label fallbacks and iframe handling.
- Persists run-scoped JSON-lines logs (redacted) for step-level auditing and selector stats for recoverability.
- Falls back to adapters for API ticket creation when UI flow hits SSO/login or missing required fields.
"""
from __future__ import annotations
import os
import time
import logging
import pathlib
import traceback
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# reuse adapters for API fallback
from adapters import zendesk_adapter, freshdesk_adapter

# reuse parser LLM wrappers
import parser as parser_mod

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_TIMEOUT = int(os.getenv("UI_AGENT_TIMEOUT_MS", "45000"))
SHORT_TIMEOUT = int(os.getenv("UI_AGENT_SHORT_TIMEOUT_MS", "8000"))

PROVIDERS_DIR = pathlib.Path("providers")
LOG_DIR = pathlib.Path(os.getenv("UI_AGENT_DIAG_DIR", "./ui_agent_diag"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "steps.jsonl"
SELECTOR_STATS_FILE = LOG_DIR / "selector_stats.json"

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

# Default inline provider configs (fallback if providers/*.json not present)
INLINE_PROVIDER_CONFIGS: Dict[str, Dict[str, Any]] = {
    "zendesk": {
        "base_url": "https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent",
        "login": {
            "email_selectors": ["input[type='email']", "input[name='email']", "input[id*='email']", "input[placeholder*='Email']"],
            "password_selectors": ["input[type='password']", "input[name='password']", "input[id*='password']"],
            "login_button_selectors": ["button[type='submit']", "button:has-text('Sign in')", "button:has-text('Log in')"]
        },
        "open_new_selectors": ["button:has-text('Add')", "button:has-text('+ New ticket')", "button:has-text('New')", "a[href*='/agent/tickets/new']"],
        "fields": {
            "subject": ["input[placeholder='Subject']", "input[placeholder*='Subject']", "input[id*='ticket_subject']", "textarea[placeholder='Subject']", "div:has-text('Subject') input"],
            "description": ["textarea[name='comment']", "textarea[name='description']", "textarea[id*='comment']", "div[role='textbox'][contenteditable='true']", "div[contenteditable='true']"],
            "requester": ["input[placeholder*='Search or add requester']", "input[aria-label*='requester']", "input[placeholder*='Requester']", "input[name*='requester']"],
            "assignee": ["input[placeholder*='Search or add assignee']", "input[aria-label*='assignee']", "input[placeholder*='Assignee']"],
            "tags": ["input[placeholder*='Add a tag']", "input[placeholder*='Tags']"],
            "type": ["select[name='type']"],
            "priority": ["select[name='priority']"],
        },
        "submit_selectors": ["button:has-text('Submit as New')", "button:has-text('Submit')", "button:has-text('Save')", "button:has-text('Create')"]
    },
    "freshdesk": {
        "base_url": "https://{FRESHDESK_DOMAIN}.freshdesk.com/",
        "login": {
            "email_selectors": ["input[type='email']", "input[name='email']", "input[id*='email']", "input[placeholder*='Email']"],
            "password_selectors": ["input[type='password']", "input[name='password']", "input[id*='password']"],
            "login_button_selectors": ["button[type='submit']", "button:has-text('Sign in')", "button:has-text('Log in')"]
        },
        "open_new_selectors": ["a:has-text('New Ticket')", "button:has-text('New Ticket')", "button:has-text('New')", "a[href*='/a/tickets/new']"],
        "fields": {
            "subject": ["input[name='subject']", "input[id*='subject']", "input[placeholder*='Subject']"],
            "description": ["textarea[name='description']", "textarea[id*='description']", "div[role='textbox'][contenteditable='true']"],
            "requester": ["input[name='email']", "input[placeholder*='Email']", "input[id*='email']"],
            "priority": ["select[name='priority']"],
        },
        "submit_selectors": ["button:has-text('Save')", "button:has-text('Create')", "button:has-text('Submit')"]
    }
}


def _redact_text(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = _EMAIL_RE.sub("[REDACTED_EMAIL]", s)
    # common API key patterns
    s = re.sub(r"(?i)(api[_-]?key|token)[\"']?\s*[:=]\s*['\"]?[\w\-\.]{8,}['\"]?", r"\1: [REDACTED]", s)
    return s


def _redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside JSON-like structures."""
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, str):
        return _redact_text(obj)
    return obj


def _load_provider_configs() -> Dict[str, Dict[str, Any]]:
    """Load JSON provider configs from providers/ if present, else fallback to inline defaults."""
    configs = {}
    if PROVIDERS_DIR.exists() and PROVIDERS_DIR.is_dir():
        for p in PROVIDERS_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                configs[p.stem] = data
            except Exception:
                logger.debug("Failed to load provider config %s", p, exc_info=True)
    # merge with inline defaults (do not overwrite any user-provided file)
    merged = dict(INLINE_PROVIDER_CONFIGS)
    merged.update(configs)
    return merged


def _append_logline(log_obj: Dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(log_obj) + "\n")
    except Exception:
        logger.debug("Could not write logline", exc_info=True)


def _load_selector_stats() -> Dict[str, Dict[str, int]]:
    try:
        if SELECTOR_STATS_FILE.exists():
            return json.loads(SELECTOR_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Could not read selector stats", exc_info=True)
    return {}


def _save_selector_stats(stats: Dict[str, Dict[str, int]]) -> None:
    try:
        SELECTOR_STATS_FILE.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("Could not write selector stats", exc_info=True)


class GenericUIAgent:
    def __init__(self, headless: Optional[bool] = None, slow_mo: Optional[int] = None, user_data_dir: Optional[str] = None):
        self.headless = headless if headless is not None else (os.getenv("PLAYWRIGHT_HEADLESS", "true").strip().lower() not in ("0", "false", "no"))
        self.slow_mo = slow_mo if slow_mo is not None else (int(os.getenv("PLAYWRIGHT_SLOW_MO")) if os.getenv("PLAYWRIGHT_SLOW_MO") else None)
        self.user_data_dir = user_data_dir or os.getenv("PLAYWRIGHT_USER_DATA_DIR")
        self.provider_configs = _load_provider_configs()
        self.selector_stats = _load_selector_stats()
        # current run context
        self._run_id: Optional[str] = None

    # ----------------- browser mgmt --------------------------------------
    def _launch(self, p):
        if self.user_data_dir:
            ctx = p.chromium.launch_persistent_context(user_data_dir=self.user_data_dir, headless=self.headless, slow_mo=self.slow_mo or 0)
            pages = ctx.pages
            page = pages[0] if pages else ctx.new_page()
            return ctx, page, True
        browser = p.chromium.launch(headless=self.headless, slow_mo=self.slow_mo or 0)
        page = browser.new_page()
        return browser, page, False

    def _close(self, ctx_or_browser):
        try:
            ctx_or_browser.close()
        except Exception:
            logger.debug("Error closing browser/context", exc_info=True)

    # ------------- structured logging -----------------------------------
    def _log_step(self, event: str, step: Dict[str, Any], extra: Optional[Dict[str, Any]] = None):
        payload = {
            "run_id": self._run_id,
            "ts": time.time(),
            "event": event,
            "step": step
        }
        if extra:
            payload.update({"extra": extra})
        # redact before writing logs
        try:
            safe = _redact_obj(payload)
            logger.info(json.dumps(safe))
            _append_logline(safe)
        except Exception:
            logger.info("STEPLOG %s %s", event, payload)

    # ------------- diagnostics ------------------------------------------
    def save_diagnostic(self, page, provider_name: str):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            png = LOG_DIR / f"{provider_name}_{ts}.png"
            html = LOG_DIR / f"{provider_name}_{ts}.html"
            try:
                page.screenshot(path=str(png), full_page=True)
            except Exception:
                logger.debug("screenshot failed", exc_info=True)
            try:
                content = page.content()
                # redact html content minimally (emails)
                content = _redact_text(content)
                html.write_text(content, encoding="utf-8")
            except Exception:
                logger.debug("html save failed", exc_info=True)
        except Exception:
            logger.debug("diagnostic save failed", exc_info=True)

    # ------------- DOM heuristics ---------------------------------------
    def find_field_by_label(self, page, label_text: str, timeout: int = 500) -> Optional[str]:
        """
        Heuristic: try to locate an input/textarea/contenteditable near a label that contains label_text.
        If not found, try placeholder / aria-label selectors that contain the label text.
        Returns a selector string or None.
        """
        try:
            labels = page.query_selector_all("label")
            for lbl in labels:
                try:
                    txt = (lbl.inner_text() or "").strip()
                    if label_text.lower() in txt.lower():
                        # If label has a 'for' attribute, prefer that element id
                        for_attr = lbl.get_attribute("for")
                        if for_attr:
                            cand = f"#{for_attr}"
                            try:
                                page.wait_for_selector(cand, timeout=timeout)
                                return cand
                            except Exception:
                                pass
                        # Otherwise, look for sibling inputs/textarea/contenteditable
                        for sel in ["input", "textarea", "div[contenteditable='true']"]:
                            try:
                                rs = lbl.query_selector_all(f":scope ~ {sel}")
                            except Exception:
                                rs = []
                            if rs:
                                el = rs[0]
                                eid = el.get_attribute("id")
                                if eid:
                                    return f"#{eid}"
                                else:
                                    # return a generic selector relative to label scope
                                    return sel
                except Exception:
                    # ignore issues reading an individual label and continue
                    continue

            # Fallback: try placeholder / aria-label selectors containing the label text.
            # Use .format to avoid nested quote issues inside f-strings.
            cand = "input[placeholder*='{0}'], textarea[placeholder*='{0}'], input[aria-label*='{0}']".format(label_text)
            try:
                page.wait_for_selector(cand, timeout=timeout)
                return cand
            except Exception:
                return None
        except Exception:
            return None

    # ------------- robust primitives -----------------------------------
    def _score_and_order_selectors(self, selectors: List[str]) -> List[str]:
        # order selectors by historical success rate (stored in self.selector_stats)
        def score(s):
            st = self.selector_stats.get(s, {})
            tries = st.get("tries", 0)
            succ = st.get("successes", 0)
            if tries == 0:
                return 0.0
            return succ / tries
        ordered = sorted(selectors, key=lambda x: score(x), reverse=True)
        return ordered

    def _update_selector_stats(self, selector: str, success: bool):
        st = self.selector_stats.setdefault(selector, {"tries": 0, "successes": 0})
        st["tries"] += 1
        if success:
            st["successes"] += 1
        # persist stats periodically
        try:
            _save_selector_stats(self.selector_stats)
        except Exception:
            logger.debug("Could not persist selector stats", exc_info=True)

    def safe_fill(self, page, selectors, value, timeout=SHORT_TIMEOUT, per_selector_retries: int = 1) -> Tuple[bool, Optional[str]]:
        if isinstance(selectors, str):
            selectors = [selectors]
        selectors = self._score_and_order_selectors(selectors)
        for s in selectors:
            for attempt in range(per_selector_retries):
                try:
                    page.wait_for_selector(s, timeout=timeout)
                    try:
                        page.fill(s, value)
                        self._log_step("fill_success", {"selector": s})
                        self._update_selector_stats(s, True)
                        return True, s
                    except Exception:
                        try:
                            page.eval_on_selector(s, "(el, v) => { if (el.isContentEditable) el.innerText = v; else if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') el.value = v; }", value)
                            self._log_step("fill_success_eval", {"selector": s})
                            self._update_selector_stats(s, True)
                            return True, s
                        except Exception:
                            self._log_step("fill_attempt_failed", {"selector": s, "attempt": attempt + 1})
                except PlaywrightTimeoutError:
                    self._log_step("fill_selector_timeout", {"selector": s})
                except Exception:
                    self._log_step("fill_selector_error", {"selector": s, "exc": traceback.format_exc()[:400]})
                # mark failure attempt
                self._update_selector_stats(s, False)
        return False, None

    def safe_click(self, page, selectors, timeout=SHORT_TIMEOUT, per_selector_retries: int = 1) -> Tuple[bool, Optional[str]]:
        if isinstance(selectors, str):
            selectors = [selectors]
        selectors = self._score_and_order_selectors(selectors)
        for s in selectors:
            for attempt in range(per_selector_retries):
                try:
                    page.wait_for_selector(s, timeout=timeout)
                    page.click(s)
                    self._log_step("click_success", {"selector": s})
                    self._update_selector_stats(s, True)
                    return True, s
                except PlaywrightTimeoutError:
                    self._log_step("click_timeout", {"selector": s, "attempt": attempt + 1})
                except Exception:
                    self._log_step("click_error", {"selector": s, "exc": traceback.format_exc()[:400], "attempt": attempt + 1})
                self._update_selector_stats(s, False)
        return False, None

    def execute_steps(self, page, steps: List[Dict[str, Any]], intent: Dict[str, Any], per_step_retries: int = 2) -> bool:
        try:
            for idx, st in enumerate(steps):
                st_meta = dict(st); st_meta["index"] = idx
                self._log_step("step_start", st_meta)
                action = st.get("action")
                selectors = st.get("selector_candidates") or ([st.get("selector")] if st.get("selector") else [])
                if isinstance(selectors, str):
                    selectors = [selectors]

                if action == "click":
                    ok, used = self.safe_click(page, selectors, timeout=SHORT_TIMEOUT, per_selector_retries=per_step_retries)
                    self._log_step("step_end", st_meta, {"result": "ok" if ok else "failed", "selector_used": used})
                elif action == "fill":
                    val = st.get("value")
                    if not val:
                        vs = st.get("value_source") or st.get("valueFrom") or st.get("source")
                        if isinstance(vs, str) and vs.startswith("fields."):
                            try:
                                parts = vs.split("."); fld = parts[1]
                                val = intent.get("fields", {}).get(fld, {}).get("value") if intent.get("fields") else None
                            except Exception:
                                val = None
                        elif st.get("target"):
                            val = intent.get(st.get("target")) or (intent.get("fields", {}) or {}).get(st.get("target"), {}).get("value")
                    ok, used = self.safe_fill(page, selectors, val or "", timeout=SHORT_TIMEOUT, per_selector_retries=per_step_retries)
                    if not ok and st.get("target") and isinstance(st.get("target"), str):
                        try:
                            cand = self.find_field_by_label(page, st.get("target"), timeout=500)
                            if cand:
                                ok, used = self.safe_fill(page, cand, val or "", timeout=SHORT_TIMEOUT, per_selector_retries=per_step_retries)
                                if ok:
                                    self._log_step("step_fill_label_fallback", st_meta, {"selector_used": used})
                        except Exception:
                            self._log_step("step_fill_label_error", st_meta, {"exc": traceback.format_exc()[:400]})
                    self._log_step("step_end", st_meta, {"result": "ok" if ok else "failed", "selector_used": used})
                else:
                    self._log_step("step_unknown_action", st_meta)
            return True
        except Exception:
            logger.debug("Exception executing DOM steps: %s", traceback.format_exc())
            return False

    # --------- LLM-driven DOM inference (when no steps provided) -----------
    def _prepare_base_url(self, cfg: Dict[str, Any]) -> str:
        base = cfg.get("base_url", "")
        # fill placeholders from env
        try:
            base = base.format(**os.environ)
        except Exception:
            # try manual replacements for common keys
            base = base.replace("{ZENDESK_SUBDOMAIN}", os.getenv("ZENDESK_SUBDOMAIN", ""))
            base = base.replace("{FRESHDESK_DOMAIN}", os.getenv("FRESHDESK_DOMAIN", ""))
        return base

    def _redact_html_for_llm(self, html: str) -> str:
        html = _redact_text(html)
        # keep a reasonable snippet size
        return html[:120000]

    def infer_steps_with_llm(self, page, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Capture page HTML (redacted), and ask the LLM (via parser.call_groq + extract_json_from_response)
        to produce the same DOM steps JSON format used by execute_steps.
        Returns parsed dict or None on failure.
        """
        try:
            try:
                dom_html = page.content()
            except Exception:
                dom_html = "<could-not-capture-dom/>"
            dom_html = self._redact_html_for_llm(dom_html)

            instruction = intent.get("meta", {}).get("instruction") or intent.get("instruction") or "<no instruction provided>"

            # Example JSON structure as Python dict, then serialized safely with json.dumps
            example_struct = {
                "fields": {
                    "subject": {
                        "value": "...",
                        "selector_candidates": ["input[placeholder='Subject']"]
                    }
                },
                "steps": [
                    {"action": "click", "selector_candidates": ["button:has-text('New')"]},
                    {"action": "fill", "target": "subject", "selector_candidates": ["input[placeholder='Subject']"], "value_source": "fields.subject.value"}
                ]
            }

            # Build prompt safely using a triple-quoted f-string and json.dumps for the example
            prompt = f"""You are a UI assistant that outputs JSON describing form fields and DOM interaction steps
for filling a ticket form in a web app. Only output valid JSON. Keys: fields (mapping logical names -> {{value, selector_candidates}})
and steps (ordered list of {{action:'click'|'fill', selector_candidates:[...], target|'value_source' ...}}).

INSTRUCTION:
{instruction}

PAGE_HTML_SNIPPET (redacted):
{dom_html}

Return compact JSON. Example structure:
{json.dumps(example_struct)}
"""

            # Call the LLM via the existing parser wrapper and extract JSON safely
            try:
                resp = parser_mod.call_groq(prompt, timeout=30)
                parsed = parser_mod.extract_json_from_response(resp)
                if not isinstance(parsed, dict):
                    return None
                if "steps" in parsed or "fields" in parsed:
                    return parsed
                return None
            except Exception:
                logger.debug("LLM inference call or parsing failed", exc_info=True)
                return None

        except Exception:
            logger.debug("infer_steps_with_llm encountered an unexpected error", exc_info=True)
            return None

    # ------------ unified, config-driven create_ticket -------------------
    def create_ticket(self, provider: str, intent: Dict[str, Any], dry_run: bool = False, headless: Optional[bool] = None, slow_mo: Optional[int] = None, timeout: Optional[int] = None) -> Dict[str, Any]:
        provider = provider.lower()
        cfg = self.provider_configs.get(provider)
        if not cfg:
            return {"status": "error", "error": f"no config for provider: {provider}"}
        timeout = timeout or DEFAULT_TIMEOUT

        base_url = self._prepare_base_url(cfg)
        result: Dict[str, Any] = {"status": "error", "error": None, "url": None, "ticket_id": None, "api_result": None}

        # new run
        self._run_id = str(uuid.uuid4())
        self._log_step("run_start", {"provider": provider, "intent_summary": _redact_obj({"subject": intent.get("subject"), "requester": intent.get("requester")})})

        agent = GenericUIAgent(headless=headless if headless is not None else self.headless, slow_mo=slow_mo if slow_mo is not None else self.slow_mo, user_data_dir=self.user_data_dir)

        try:
            with sync_playwright() as p:
                ctx_or_browser, page, persistent = agent._launch(p)
                try:
                    if base_url:
                        try:
                            page.goto(base_url, wait_until="load", timeout=timeout)
                        except Exception:
                            try:
                                page.goto(base_url, wait_until="domcontentloaded", timeout=timeout)
                            except Exception:
                                pass

                    # Generic login attempt using configured selectors
                    login_cfg = cfg.get("login", {})
                    email_selectors = login_cfg.get("email_selectors", [])
                    password_selectors = login_cfg.get("password_selectors", [])
                    login_button_selectors = login_cfg.get("login_button_selectors", [])
                    email = os.getenv(f"{provider.upper()}_EMAIL") or os.getenv("ZENDESK_EMAIL") or os.getenv("FRESHDESK_EMAIL")
                    password = os.getenv(f"{provider.upper()}_PASSWORD") or os.getenv("ZENDESK_PASSWORD") or os.getenv("FRESHDESK_PASSWORD")
                    try:
                        if email_selectors:
                            page.wait_for_selector(", ".join(email_selectors), timeout=SHORT_TIMEOUT)
                            logger.info("Login UI detected for %s; attempting generic credential fill", provider)
                            if email and agent.safe_fill(page, email_selectors, email, timeout=SHORT_TIMEOUT)[0]:
                                if password and password_selectors:
                                    agent.safe_fill(page, password_selectors, password, timeout=SHORT_TIMEOUT)
                                if login_button_selectors:
                                    agent.safe_click(page, login_button_selectors, timeout=SHORT_TIMEOUT)
                                page.wait_for_load_state("load", timeout=timeout)
                            else:
                                logger.info("Detected login area but could not auto-fill credentials for %s", provider)
                    except PlaywrightTimeoutError:
                        logger.debug("No immediate login found for %s (may be SSO or already logged in)", provider)

                    # If intent already has steps, prefer them
                    if intent.get("steps"):
                        if dry_run:
                            return {"status": "dry-run", "steps": intent.get("steps")}
                        ok = agent.execute_steps(page, intent.get("steps", []), intent)
                        time.sleep(1.0)
                        result_url = page.url
                        if ok:
                            result.update({"status": "ok", "url": result_url, "message": "Executed provided DOM steps; verify UI."})
                            try:
                                m = re.search(r"/tickets/(\d+)", result_url)
                                if m:
                                    result["ticket_id"] = int(m.group(1))
                            except Exception:
                                pass
                            try:
                                agent.save_diagnostic(page, f"{provider}_steps_executed")
                            except Exception:
                                pass
                            return result
                        logger.info("Provided DOM steps failed or incomplete; falling back to inference/heuristics")

                    # Attempt to infer steps using LLM from the live DOM (high-impact)
                    inferred = None
                    try:
                        inferred = self.infer_steps_with_llm(page, intent)
                    except Exception:
                        inferred = None

                    if inferred and isinstance(inferred, dict) and inferred.get("steps"):
                        # attach and execute inferred steps
                        intent.setdefault("fields", {}).update(inferred.get("fields", {}))
                        intent["steps"] = inferred["steps"]
                        self._log_step("inferred_steps_attached", {"count": len(inferred["steps"])})
                        if dry_run:
                            return {"status": "dry-run", "steps": intent.get("steps")}
                        ok = agent.execute_steps(page, intent.get("steps", []), intent)
                        time.sleep(1.0)
                        result_url = page.url
                        if ok:
                            result.update({"status": "ok", "url": result_url, "message": "Executed LLM-inferred DOM steps; verify UI."})
                            try:
                                m = re.search(r"/tickets/(\d+)", result_url)
                                if m:
                                    result["ticket_id"] = int(m.group(1))
                            except Exception:
                                pass
                            try:
                                agent.save_diagnostic(page, f"{provider}_inferred_steps_executed")
                            except Exception:
                                pass
                            return result
                        logger.info("LLM-inferred steps execution failed; falling back to config-driven heuristics")

                    # --- Config-driven heuristic flow: open 'new' and fill fields ---
                    opened = False
                    for s in cfg.get("open_new_selectors", []):
                        opened, _ = agent.safe_click(page, s, timeout=SHORT_TIMEOUT)
                        if opened:
                            break
                    if not opened:
                        try:
                            if provider == "zendesk":
                                page.goto(f"https://{os.getenv('ZENDESK_SUBDOMAIN','')}.zendesk.com/agent/tickets/new", wait_until="load", timeout=timeout)
                            elif provider == "freshdesk":
                                page.goto(f"https://{os.getenv('FRESHDESK_DOMAIN','')}.freshdesk.com/a/tickets/new", wait_until="load", timeout=timeout)
                        except Exception:
                            logger.debug("direct new ticket path failed", exc_info=True)

                    fields_cfg = cfg.get("fields", {})
                    subject = intent.get("subject", "")
                    if "subject" in fields_cfg:
                        ok, used = agent.safe_fill(page, fields_cfg["subject"], subject, timeout=SHORT_TIMEOUT)
                        if not ok:
                            lbl = agent.find_field_by_label(page, "subject")
                            if lbl:
                                agent.safe_fill(page, lbl, subject, timeout=SHORT_TIMEOUT)

                    requester_email = intent.get("requester", {}).get("email") if isinstance(intent.get("requester"), dict) else (intent.get("requester") or "")
                    if "requester" in fields_cfg and requester_email:
                        ok, used = agent.safe_fill(page, fields_cfg["requester"], requester_email, timeout=SHORT_TIMEOUT)
                        if not ok:
                            lbl = agent.find_field_by_label(page, "requester")
                            if lbl:
                                agent.safe_fill(page, lbl, requester_email, timeout=SHORT_TIMEOUT)
                        try:
                            page.wait_for_selector("div[role='option'], li[role='option'], .ember-power-select-option", timeout=SHORT_TIMEOUT)
                            agent.safe_click(page, ["div[role='option']", "li[role='option']", ".ember-power-select-option"], timeout=SHORT_TIMEOUT)
                        except PlaywrightTimeoutError:
                            pass

                    description = intent.get("description", "") or ""
                    if "description" in fields_cfg:
                        ok, used = agent.safe_fill(page, fields_cfg["description"], description, timeout=SHORT_TIMEOUT)
                        if not ok:
                            lbl = agent.find_field_by_label(page, "description")
                            if lbl:
                                agent.safe_fill(page, lbl, description, timeout=SHORT_TIMEOUT)
                        try:
                            for f in page.frames:
                                try:
                                    f.wait_for_selector("body", timeout=SHORT_TIMEOUT)
                                    try:
                                        f.fill("body", description)
                                        break
                                    except Exception:
                                        continue
                                except PlaywrightTimeoutError:
                                    continue
                        except Exception:
                            logger.debug("iframe handling error", exc_info=True)

                    tags = intent.get("tags", []) or []
                    if tags and "tags" in fields_cfg:
                        for t in tags:
                            ok, used = agent.safe_fill(page, fields_cfg["tags"], t, timeout=SHORT_TIMEOUT)
                            if ok:
                                agent.safe_click(page, ["button:has-text('+ Add Tag')", "button:has-text('Add tag')"], timeout=SHORT_TIMEOUT)

                    if "type" in fields_cfg and intent.get("type"):
                        try:
                            page.select_option("select[name='type']", label=intent.get("type"))
                        except Exception:
                            agent.safe_click(page, [f"button:has-text('{intent.get('type')}')", f"div:has-text('{intent.get('type')}')"], timeout=SHORT_TIMEOUT)
                    if "priority" in fields_cfg and intent.get("priority"):
                        try:
                            page.select_option("select[name='priority']", label=intent.get("priority"))
                        except Exception:
                            agent.safe_click(page, [f"button:has-text('{intent.get('priority')}')", f"div:has-text('{intent.get('priority')}')"], timeout=SHORT_TIMEOUT)

                    _subject_val = (subject or "").strip()
                    _description_val = (description or "").strip()
                    if not _subject_val or not _description_val:
                        missing = []
                        if not _subject_val: missing.append("subject")
                        if not _description_val: missing.append("description")
                        logger.warning("Missing required fields after UI fill: %s", missing)
                        try:
                            agent.save_diagnostic(page, f"{provider}_missing_fields")
                        except Exception:
                            pass
                        if provider == "zendesk":
                            api_res = zendesk_adapter.create_ticket(intent)
                        else:
                            api_res = freshdesk_adapter.create_ticket(intent)
                        result["api_result"] = api_res
                        if api_res.get("status") == "ok":
                            result.update({"status": "ok", "ticket_id": api_res.get("ticket_id"), "url": api_res.get("raw", {}).get("url")})
                        else:
                            result.update({"status": "error", "error": "Missing required fields and API fallback failed", "missing": missing, "api_result": api_res})
                        return result

                    submitted = False
                    for s in cfg.get("submit_selectors", []):
                        submitted, used = agent.safe_click(page, s, timeout=SHORT_TIMEOUT)
                        if submitted:
                            break
                    if not submitted:
                        try:
                            page.keyboard.press("Enter")
                        except Exception:
                            pass

                    time.sleep(1.2)
                    result_url = page.url
                    if any(x in result_url.lower() for x in ("/support/login", "/sso", "/login", "accounts.google.com", "microsoftonline.com")):
                        logger.info("Detected login/SSO or external auth after submit; attempting API fallback")
                        try:
                            agent.save_diagnostic(page, f"{provider}_sso")
                        except Exception:
                            pass
                        if provider == "zendesk":
                            api_res = zendesk_adapter.create_ticket(intent)
                        else:
                            api_res = freshdesk_adapter.create_ticket(intent)
                        result["api_result"] = api_res
                        if api_res.get("status") == "ok":
                            result.update({"status": "ok", "ticket_id": api_res.get("ticket_id"), "url": api_res.get("raw", {}).get("url"), "message": "Created via API fallback"})
                            return result
                        else:
                            result.update({"status": "error", "error": "UI failed and API fallback failed", "url": result_url})
                            return result

                    result.update({"status": "ok", "url": result_url, "message": "Attempted UI ticket creation; verify UI."})
                    return result

                finally:
                    agent._close(ctx_or_browser)
        except PlaywrightTimeoutError as te:
            logger.exception("Timeout while creating ticket for %s: %s", provider, te)
            if provider == "zendesk":
                api_try = zendesk_adapter.create_ticket(intent)
            else:
                api_try = freshdesk_adapter.create_ticket(intent)
            if api_try.get("status") == "ok":
                return {"status": "ok", "ticket_id": api_try.get("ticket_id"), "url": api_try.get("raw", {}).get("url"), "message": "Timeout in UI; created via API fallback", "api_result": api_try}
            return {"status": "error", "error": "Timeout while interacting with UI: " + str(te), "api_result": api_try}
        except Exception as e:
            logger.exception("Unhandled error in create_ticket for %s: %s", provider, e)
            if provider == "zendesk":
                api_try = zendesk_adapter.create_ticket(intent)
            else:
                api_try = freshdesk_adapter.create_ticket(intent)
            if api_try.get("status") == "ok":
                return {"status": "ok", "ticket_id": api_try.get("ticket_id"), "url": api_try.get("raw", {}).get("url"), "message": "UI raised error; created via API fallback", "api_result": api_try}
            return {"status": "error", "error": str(e), "api_result": api_try}
