#!/usr/bin/env python3
# agents/generic_ui_agent.py
"""
GenericUIAgent (config-driven).

- IMAP-based passcode (2FA) retrieval (preferred for Gmail reliability).
- Browser-inbox passcode fallback (best-effort).
- Automated username/password login attempts using provider login selectors.
- SSO detection with manual-wait + adapter fallback.
- Multi-strategy submit (selectors, frames, JS click, keyboard, form.submit()) with disabled-attribute workaround.
- Optional OCR (pytesseract) for screenshot -> text augmentation to LLM.
- LLM-chain inference: includes a redacted DOM snippet and optional OCR text when calling LLM.
- Selector scoring and persistence, structured redacted logs, diagnostics saving.
- Adapter resolution via adapters.get_adapter registry (providers can be pluggable).
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

from adapters import get_adapter  # dynamic adapter registry (best-effort)
import parser as parser_mod

# optional OCR
try:
    from PIL import Image
    import pytesseract
    _OCR_AVAILABLE = True
except Exception:
    _OCR_AVAILABLE = False

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_TIMEOUT = int(os.getenv("UI_AGENT_TIMEOUT_MS", "45000"))
SHORT_TIMEOUT = int(os.getenv("UI_AGENT_SHORT_TIMEOUT_MS", "8000"))
SSO_MANUAL_WAIT_MS = int(os.getenv("UI_AGENT_SSO_MANUAL_WAIT_MS", str(60 * 1000)))  # default 60s
POST_PASSCODE_WAIT_MS = int(os.getenv("UI_AGENT_POST_PASSCODE_WAIT_MS", str(20000)))  # default 20s

PROVIDERS_DIR = pathlib.Path("providers")
LOG_DIR = pathlib.Path(os.getenv("UI_AGENT_DIAG_DIR", "./ui_agent_diag"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "steps.jsonl"
SELECTOR_STATS_FILE = LOG_DIR / "selector_stats.json"

_SAVE_RAW_DIAGNOSTICS = os.getenv("UI_AGENT_SAVE_RAW_DIAGNOSTICS", "false").strip().lower() in ("1", "true", "yes")
_DEBUG_RETURN_RAW = os.getenv("DEBUG_RETURN_RAW_API_RESPONSES", "false").strip().lower() in ("1", "true", "yes")

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_SSO_BUTTON_PATTERNS = [
    "Sign in with", "Continue with", "Sign in using", "Sign in to your", "Use SSO", "Single Sign-On",
    "Sign in with Google", "Sign in with Microsoft", "Sign in with Okta", "Sign in with SSO"
]


def _redact_text(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = _EMAIL_RE.sub("[REDACTED_EMAIL]", s)
    s = re.sub(r"(?i)(api[_-]?key|token)[\"']?\s*[:=]\s*['\"]?[\w\-\.]{8,}['\"]?", r"\1: [REDACTED]", s)
    return s


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, str):
        return _redact_text(obj)
    return obj


def _load_provider_configs() -> Dict[str, Dict[str, Any]]:
    configs: Dict[str, Dict[str, Any]] = {}
    if not PROVIDERS_DIR.exists() or not PROVIDERS_DIR.is_dir():
        raise RuntimeError("providers directory not found: providers/ -- it must contain provider JSON files (e.g. zendesk.json, freshdesk.json)")
    for p in PROVIDERS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            configs[p.stem] = data
        except Exception:
            logger.debug("Failed to load provider config %s", p, exc_info=True)
    if not configs:
        raise RuntimeError("No provider JSON files found in providers/; at least one provider config is required")
    return configs


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
        self._run_id: Optional[str] = None

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

    def _log_step(self, event: str, step: Dict[str, Any], extra: Optional[Dict[str, Any]] = None):
        payload = {"run_id": self._run_id, "ts": time.time(), "event": event, "step": step}
        if extra:
            payload.update({"extra": extra})
        try:
            safe = _redact_obj(payload)
            logger.info(json.dumps(safe))
            _append_logline(safe)
        except Exception:
            logger.info("STEPLOG %s %s", event, payload)

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
                if not _SAVE_RAW_DIAGNOSTICS:
                    content = _redact_text(content)
                html.write_text(content, encoding="utf-8")
            except Exception:
                logger.debug("html save failed", exc_info=True)
        except Exception:
            logger.debug("diagnostic save failed", exc_info=True)

    def _screenshot_to_text(self, page) -> str:
        if not _OCR_AVAILABLE:
            return ""
        try:
            tmp = LOG_DIR / f"ocr_{int(time.time())}.png"
            page.screenshot(path=str(tmp), full_page=True)
            text = pytesseract.image_to_string(Image.open(str(tmp)))
            return text[:120000]
        except Exception:
            logger.debug("OCR failed", exc_info=True)
            return ""

    def find_field_by_label(self, page, label_text: str, timeout: int = 500) -> Optional[str]:
        try:
            labels = page.query_selector_all("label")
            for lbl in labels:
                try:
                    txt = (lbl.inner_text() or "").strip()
                    if label_text.lower() in txt.lower():
                        for_attr = lbl.get_attribute("for")
                        if for_attr:
                            cand = f"#{for_attr}"
                            try:
                                page.wait_for_selector(cand, timeout=timeout)
                                return cand
                            except Exception:
                                pass
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
                                    return sel
                except Exception:
                    continue
            cand = "input[placeholder*='{0}'], textarea[placeholder*='{0}'], input[aria-label*='{0}']".format(label_text)
            try:
                page.wait_for_selector(cand, timeout=timeout)
                return cand
            except Exception:
                return None
        except Exception:
            return None

    def _score_and_order_selectors(self, selectors: List[str]) -> List[str]:
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
                        try:
                            page.dispatch_event(s, "input", {"data": value})
                        except Exception:
                            pass
                        try:
                            page.dispatch_event(s, "change")
                        except Exception:
                            pass
                        try:
                            page.dispatch_event(s, "blur")
                        except Exception:
                            pass
                        try:
                            page.focus(s)
                            page.press(s, "Tab")
                        except Exception:
                            pass
                        try:
                            page.wait_for_timeout(200)
                        except Exception:
                            pass
                        self._log_step("fill_success", {"selector": s})
                        self._update_selector_stats(s, True)
                        return True, s
                    except Exception:
                        try:
                            page.eval_on_selector(s, "(el, v) => { if (el.isContentEditable) el.innerText = v; else if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }", value)
                            try:
                                page.wait_for_timeout(200)
                            except Exception:
                                pass
                            self._log_step("fill_success_eval", {"selector": s})
                            self._update_selector_stats(s, True)
                            return True, s
                        except Exception:
                            self._log_step("fill_attempt_failed", {"selector": s, "attempt": attempt + 1})
                except PlaywrightTimeoutError:
                    self._log_step("fill_selector_timeout", {"selector": s})
                except Exception:
                    self._log_step("fill_selector_error", {"selector": s, "exc": traceback.format_exc()[:400]})
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

    def _attempt_submit(self, page, cfg: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        candidates = []
        if cfg:
            candidates += cfg.get("submit_selectors", []) or []
        candidates += [
            "button:has-text('Submit as New')",
            "button:has-text('Submit')",
            "button:has-text('Save')",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Create')",
            "button[aria-label='Submit']"
        ]
        seen = set()
        dedup = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                dedup.append(c)

        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            frames = [page] + list(page.frames)
        except Exception:
            frames = [page]

        for frame in frames:
            for s in dedup:
                try:
                    try:
                        if not frame.query_selector(s):
                            continue
                    except Exception:
                        pass

                    try:
                        frame.wait_for_selector(s, timeout=SHORT_TIMEOUT)
                    except Exception:
                        pass

                    try:
                        disabled = frame.eval_on_selector(s, "el => !!(el.disabled || el.getAttribute && el.getAttribute('aria-disabled')==='true')")
                    except Exception:
                        disabled = False
                    if disabled:
                        try:
                            frame.eval_on_selector(s, "el => { try { if (el.hasAttribute && el.hasAttribute('disabled')) el.removeAttribute('disabled'); if (el.getAttribute && el.getAttribute('aria-disabled')==='true') el.setAttribute('aria-disabled','false'); } catch(e){} }")
                        except Exception:
                            pass

                    try:
                        frame.click(s, timeout=SHORT_TIMEOUT)
                        self._log_step("submit_clicked_frame", {"frame": getattr(frame, 'name', '<frame>'), "selector": s})
                        return True, s
                    except Exception:
                        try:
                            frame.eval_on_selector(s, "el => el.click()")
                            self._log_step("submit_js_click_frame", {"frame": getattr(frame, 'name', '<frame>'), "selector": s})
                            return True, s
                        except Exception:
                            continue
                except Exception:
                    continue

        try:
            try:
                page.keyboard.down("Control")
                page.keyboard.press("Enter")
                page.keyboard.up("Control")
            except Exception:
                page.keyboard.press("Enter")
            self._log_step("submit_keyboard_attempt", {})
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            return True, "keyboard"
        except Exception:
            pass

        try:
            res = page.evaluate("""() => {
                const f = document.querySelector('form');
                if (f) { try { f.submit(); return true } catch(e) { return false } }
                return false;
            }""")
            if res:
                self._log_step("submit_form_submit", {})
                return True, "form.submit()"
        except Exception:
            pass

        return False, None

    # ---------------- PASSCODE (IMAP + browser fallback) helpers ----------------
    def _detect_passcode_prompt(self, page, cfg: Optional[Dict[str, Any]] = None) -> bool:
        try:
            selectors = []
            if isinstance(cfg, dict) and cfg.get("passcode_selectors"):
                selectors = cfg.get("passcode_selectors") or []
            selectors += [
                "input[type='tel']",
                "input[type='text'][inputmode='numeric']",
                "input[name*='code']",
                "input[id*='code']",
                "input[name*='otp']",
                "input[id*='otp']",
                "input[placeholder*='code']",
                "input[placeholder*='OTP']",
                "input[aria-label*='code']",
            ]
            for s in selectors:
                try:
                    el = page.query_selector(s)
                    if el:
                        return True
                except Exception:
                    continue
            try:
                body = page.inner_text("body")[:3000].lower()
                if any(t in body for t in ("enter the code", "enter the passcode", "verification code", "one-time code", "we sent a code", "verify you")):
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _fetch_passcode_via_imap(self, cfg: Optional[Dict[str, Any]] = None, timeout_seconds: int = 30, poll_interval: float = 3.0) -> Optional[str]:
        host = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
        port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
        user = os.getenv("EMAIL_IMAP_USER")
        password = os.getenv("EMAIL_IMAP_PASSWORD")
        if not user or not password:
            logger.debug("IMAP credentials not configured; skipping IMAP passcode fetch")
            return None

        subject_re = os.getenv("EMAIL_PASSCODE_SUBJECT_REGEX") or (cfg.get("passcode_email_subject_regex") if isinstance(cfg, dict) else None)
        if not subject_re:
            subject_re = r"(verification code|security code|one-time code|passcode|verification)"

        code_re = os.getenv("EMAIL_PASSCODE_REGEX") or (cfg.get("passcode_regex") if isinstance(cfg, dict) else None)
        if not code_re:
            code_re = r"(\d{4,8})"

        deadline = time.time() + float(timeout_seconds)
        try:
            import imaplib, email as _email_lib
        except Exception:
            logger.debug("imaplib/email not available", exc_info=True)
            return None

        while time.time() < deadline:
            try:
                imap = imaplib.IMAP4_SSL(host, port)
                try:
                    imap.login(user, password)
                except Exception as e:
                    logger.debug("IMAP login failed: %s", e)
                    try:
                        imap.logout()
                    except Exception:
                        pass
                    return None

                folder = os.getenv("EMAIL_IMAP_FOLDER", "INBOX")
                try:
                    imap.select(folder)
                except Exception:
                    try:
                        imap.select()
                    except Exception:
                        pass

                typ, data = imap.search(None, "ALL")
                if typ != "OK":
                    try:
                        imap.logout()
                    except Exception:
                        pass
                    time.sleep(poll_interval)
                    continue

                ids = data[0].split()
                recent_ids = ids[-50:] if ids else []
                for mid in reversed(recent_ids):
                    try:
                        typ, msg_data = imap.fetch(mid, "(RFC822)")
                        if typ != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1]
                        if not raw:
                            continue
                        msg = _email_lib.message_from_bytes(raw)
                        subj = (msg.get("Subject") or "")
                        if re.search(subject_re, subj, re.IGNORECASE):
                            body_text = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    ctype = part.get_content_type()
                                    disp = str(part.get("Content-Disposition") or "")
                                    if ctype == "text/plain" and "attachment" not in disp:
                                        try:
                                            payload = part.get_payload(decode=True)
                                            if payload:
                                                body_text += payload.decode(errors="ignore")
                                        except Exception:
                                            pass
                            else:
                                try:
                                    payload = msg.get_payload(decode=True)
                                    if payload:
                                        body_text = payload.decode(errors="ignore")
                                except Exception:
                                    body_text = ""
                            m = re.search(code_re, body_text)
                            if m:
                                code = m.group(1)
                                try:
                                    imap.logout()
                                except Exception:
                                    pass
                                return code
                    except Exception:
                        continue
                try:
                    imap.logout()
                except Exception:
                    pass
            except Exception:
                logger.debug("IMAP fetch exception: %s", traceback.format_exc()[:400])
            time.sleep(poll_interval)
        return None

    def _fetch_passcode_from_inbox(self, page, cfg: Optional[Dict[str, Any]] = None, timeout_ms: int = 20000) -> Optional[str]:
        try:
            inbox_url = os.getenv("EMAIL_INBOX_URL") or (cfg.get("inbox_url") if isinstance(cfg, dict) else None)
            if not inbox_url:
                inbox_url = "https://mail.google.com/mail/u/0/#inbox"

            subject_regex = os.getenv("EMAIL_PASSCODE_SUBJECT_REGEX") or (cfg.get("passcode_email_subject_regex") if isinstance(cfg, dict) else None)
            if not subject_regex:
                subject_regex = r"(verification code|security code|one-time code|passcode|verification)"
            code_regex = os.getenv("EMAIL_PASSCODE_REGEX") or (cfg.get("passcode_regex") if isinstance(cfg, dict) else None)
            if not code_regex:
                code_regex = r"(\d{4,8})"

            ctx = page.context
            inbox_page = ctx.new_page()
            try:
                inbox_page.goto(inbox_url, timeout=timeout_ms)
            except Exception:
                pass

            try:
                inbox_page.wait_for_timeout(1500)
            except Exception:
                pass

            found = False
            try:
                found = inbox_page.evaluate(
                    """(subReStr) => {
                        try {
                            const re = new RegExp(subReStr, "i");
                            const nodes = Array.from(document.querySelectorAll('a,div,span,td,tr'));
                            for (const n of nodes) {
                                try {
                                    const txt = (n.innerText || '').trim();
                                    if (txt && re.test(txt)) {
                                        n.scrollIntoView({block:'center', inline:'center'});
                                        n.click();
                                        return true;
                                    }
                                } catch(e) { continue; }
                            }
                            return false;
                        } catch(e) { return false; }
                    }""",
                    subject_regex,
                )
            except Exception:
                found = False

            if not found:
                try:
                    content = inbox_page.content()
                    if re.search(subject_regex, content, re.IGNORECASE):
                        try:
                            inbox_page.evaluate(
                                """(subReStr) => {
                                    const re = new RegExp(subReStr, "i");
                                    const nodes = Array.from(document.querySelectorAll('a,div,span,td,tr'));
                                    for (const n of nodes) {
                                        try {
                                            const txt = (n.innerText || '').trim();
                                            if (txt && re.test(txt)) {
                                                n.scrollIntoView({block:'center', inline:'center'});
                                                n.click();
                                                return true;
                                            }
                                        } catch(e) { continue; }
                                    }
                                    return false;
                                }""",
                                subject_regex,
                            )
                            found = True
                        except Exception:
                            found = False
                except Exception:
                    found = False

            if not found:
                try:
                    inbox_page.evaluate(
                        """() => {
                            const candidates = document.querySelectorAll('tr, .zA, .message-list-item, .mailListItem, .Row');
                            if (candidates && candidates.length>0) {
                                candidates[0].scrollIntoView({block:'center'});
                                candidates[0].click();
                                return true;
                            }
                            return false;
                        }"""
                    )
                    try:
                        inbox_page.wait_for_timeout(800)
                    except Exception:
                        pass
                except Exception:
                    pass

            try:
                inbox_page.wait_for_timeout(1200)
            except Exception:
                pass

            try:
                body_text = ""
                try:
                    body_text = inbox_page.inner_text("body")[:300000]
                except Exception:
                    body_text = inbox_page.content()[:300000]
                m = re.search(code_regex, body_text)
                if m:
                    code = m.group(1)
                    try:
                        inbox_page.close()
                    except Exception:
                        pass
                    return code
            except Exception:
                pass

            try:
                html = inbox_page.content()
                m = re.search(code_regex, html)
                if m:
                    code = m.group(1)
                    try:
                        inbox_page.close()
                    except Exception:
                        pass
                    return code
            except Exception:
                pass

            try:
                inbox_page.close()
            except Exception:
                pass
            return None
        except Exception:
            try:
                inbox_page.close()
            except Exception:
                pass
            return None

    def _wait_for_login_completion(self, page, cfg: Optional[Dict[str, Any]] = None, timeout_ms: int = POST_PASSCODE_WAIT_MS) -> bool:
        """
        Wait for any provider 'open_new_selectors' or other post-login signals to appear.
        """
        start = time.time()
        selectors = []
        if isinstance(cfg, dict):
            selectors += cfg.get("open_new_selectors", []) or []
        # common heuristics
        selectors += ["button:has-text('Add')", "button:has-text('+ New ticket')", "button:has-text('New')", "a[href*='/agent/tickets/new']", "a:has-text('New Ticket')", "button:has-text('New Ticket')"]
        while time.time() - start <= (timeout_ms / 1000.0):
            try:
                for s in selectors:
                    try:
                        if page.query_selector(s):
                            self._log_step("login_confirmed", {"selector": s})
                            return True
                    except Exception:
                        continue
                # also check for navigation away from login page
                try:
                    url = page.url or ""
                    if url and not any(x in url for x in ("login", "signin", "verify", "mfa", "auth")):
                        # best-effort heuristic: assume logged in if URL changed to a non-login page
                        self._log_step("login_confirmed_by_url", {"url": url})
                        return True
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
            except Exception:
                pass
        self._log_step("login_confirm_timeout", {"timeout_ms": timeout_ms})
        return False

    # ---------------- LLM-driven DOM inference ----------------
    def _prepare_base_url(self, cfg: Dict[str, Any]) -> str:
        base = cfg.get("base_url", "") if isinstance(cfg, dict) else ""
        try:
            base = base.format(**os.environ)
        except Exception:
            base = base.replace("{ZENDESK_SUBDOMAIN}", os.getenv("ZENDESK_SUBDOMAIN", ""))
            base = base.replace("{FRESHDESK_DOMAIN}", os.getenv("FRESHDESK_DOMAIN", ""))
        return base

    def _redact_html_for_llm(self, html: str) -> str:
        html = _redact_text(html)
        return html[:120000]

    def _detect_sso_on_page(self, page) -> bool:
        try:
            content = ""
            try:
                content = page.inner_text("body")[:8000]
            except Exception:
                content = page.content()[:8000]
            for pat in _SSO_BUTTON_PATTERNS:
                if pat.lower() in content.lower():
                    return True
            return False
        except Exception:
            return False

    def infer_steps_with_llm(self, page, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            use_mock_dom = os.getenv("USE_MOCK_DOM", "true").strip().lower() not in ("0", "false", "no")
        except Exception:
            use_mock_dom = True

        if use_mock_dom:
            return None

        try:
            html = page.content()
            redacted_html = self._redact_html_for_llm(html)
        except Exception:
            redacted_html = "<could-not-capture-html>"

        ocr_text = ""
        try:
            if os.getenv("UI_AGENT_USE_OCR", "false").strip().lower() in ("1", "true", "yes"):
                ocr_text = self._screenshot_to_text(page)
        except Exception:
            ocr_text = ""

        instruction = intent.get("meta", {}).get("instruction", "") or intent.get("subject", "")
        prompt = (
            "Produce a single JSON object containing 'fields' and 'steps' for executing the instruction on the given page.\n"
            "fields: map of logical field name -> {value, selector_candidates}.\n"
            "steps: ordered list of actions with 'action'(click|fill), selector_candidates (list), and 'value_source' if needed.\n\n"
            "Page HTML (redacted):\n"
            + redacted_html[:60000]
            + "\n\nOCR text (if any):\n"
            + (ocr_text[:20000] if ocr_text else "")
            + "\n\nUser instruction:\n"
            + instruction
            + "\n\nReturn only JSON with keys 'fields' and/or 'steps'."
        )

        try:
            resp = parser_mod.call_groq(prompt, timeout=60)
            parsed = parser_mod.extract_json_from_response(resp)
            if isinstance(parsed, dict):
                steps = parsed.get("steps")
                fields = parsed.get("fields")
                result = {}
                if steps:
                    result["steps"] = steps
                if fields:
                    result["fields"] = fields
                if result:
                    return result
            return None
        except Exception:
            logger.debug("LLM-based inference failed", exc_info=True)
            return None

    def _attempt_login(self, page, provider: str, cfg: Dict[str, Any], timeout: int = 15000) -> bool:
        try:
            login_cfg = cfg.get("login") if isinstance(cfg, dict) else None
            if not login_cfg:
                return False

            provider = provider.lower()
            if provider == "zendesk":
                email = os.getenv("ZENDESK_EMAIL")
                pwd = os.getenv("ZENDESK_PASSWORD") or os.getenv("ZENDESK_API_TOKEN")
            elif provider == "freshdesk":
                email = os.getenv("FRESHDESK_EMAIL")
                pwd = os.getenv("FRESHDESK_PASSWORD") or os.getenv("FRESHDESK_API_KEY")
            else:
                email = os.getenv(f"{provider.upper()}_EMAIL")
                pwd = os.getenv(f"{provider.upper()}_PASSWORD")

            if not email or not pwd:
                return False

            for sel in login_cfg.get("email_selectors", []):
                try:
                    page.wait_for_selector(sel, timeout=2000)
                    page.fill(sel, email)
                    self._log_step("login_fill_email", {"selector": sel})
                    break
                except Exception:
                    continue

            for sel in login_cfg.get("password_selectors", []):
                try:
                    page.wait_for_selector(sel, timeout=2000)
                    page.fill(sel, pwd)
                    self._log_step("login_fill_password", {"selector": sel})
                    break
                except Exception:
                    continue

            for sel in login_cfg.get("login_button_selectors", []):
                try:
                    page.click(sel)
                    self._log_step("login_click", {"selector": sel})
                    try:
                        page.wait_for_load_state(timeout=timeout)
                    except Exception:
                        pass

                    try:
                        # small sleep to allow OTP / passcode UI to render
                        try:
                            page.wait_for_timeout(800)
                        except Exception:
                            pass

                        # If a passcode prompt appears, attempt to resolve it
                        if self._detect_passcode_prompt(page, cfg):
                            self._log_step("passcode_prompt_detected", {"provider": provider})

                            # Try IMAP first (recommended)
                            self._log_step("passcode_fetch_attempted", {"method": "imap"})
                            passcode = self._fetch_passcode_via_imap(cfg=cfg, timeout_seconds=int(os.getenv("EMAIL_IMAP_POLL_TIMEOUT_S", "30")), poll_interval=float(os.getenv("EMAIL_IMAP_POLL_INTERVAL_S", "3.0")))
                            if passcode:
                                self._log_step("passcode_fetched_imap", {"provider": provider})
                            else:
                                # fallback to browser-inbox scraping
                                self._log_step("passcode_fetch_attempted", {"method": "inbox"})
                                passcode = self._fetch_passcode_from_inbox(page, cfg)
                                if passcode:
                                    self._log_step("passcode_fetched_inbox", {"provider": provider})

                            if passcode:
                                pass_selectors = (cfg.get("passcode_selectors") or []) if isinstance(cfg, dict) else []
                                if not pass_selectors:
                                    pass_selectors = ["input[name='otp']", "input[type='tel']", "input[name*='code']", "input[id*='code']"]
                                filled = False
                                for ps in pass_selectors:
                                    try:
                                        if page.query_selector(ps):
                                            page.fill(ps, passcode)
                                            try:
                                                page.dispatch_event(ps, "input", {"data": passcode})
                                            except Exception:
                                                pass
                                            try:
                                                page.dispatch_event(ps, "change")
                                            except Exception:
                                                pass
                                            filled = True
                                            self._log_step("passcode_filled", {"selector": ps})
                                            break
                                    except Exception:
                                        continue
                                verify_selectors = (cfg.get("passcode_submit_selectors") or []) if isinstance(cfg, dict) else []
                                verify_selectors += ["button:has-text('Verify')", "button:has-text('Continue')", "button:has-text('Submit')", "button[type='submit']"]
                                for vs in verify_selectors:
                                    try:
                                        if page.query_selector(vs):
                                            try:
                                                page.click(vs)
                                                self._log_step("passcode_submit_clicked", {"selector": vs})
                                            except Exception:
                                                try:
                                                    page.eval_on_selector(vs, "el => el.click()")
                                                    self._log_step("passcode_submit_js_click", {"selector": vs})
                                                except Exception:
                                                    pass
                                            break
                                    except Exception:
                                        continue
                                try:
                                    page.wait_for_timeout(1200)
                                except Exception:
                                    pass

                                # wait for post-login UI
                                logged = self._wait_for_login_completion(page, cfg, timeout_ms=POST_PASSCODE_WAIT_MS)
                                if logged:
                                    return True
                                else:
                                    self._log_step("login_not_confirmed_after_passcode", {"provider": provider})
                                    return False
                            else:
                                self._log_step("passcode_fetch_failed", {"provider": provider})
                                return False
                        else:
                            # No passcode prompt detected; wait for login completion heuristics
                            logged = self._wait_for_login_completion(page, cfg, timeout_ms=POST_PASSCODE_WAIT_MS)
                            return logged
                    except Exception:
                        logger.debug("Exception while handling passcode: %s", traceback.format_exc())
                        return False
                except Exception:
                    continue

            return False
        except Exception:
            logger.debug("Exception in _attempt_login: %s", traceback.format_exc())
            return False

    def _resolve_adapter(self, provider: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        try:
            if isinstance(cfg, dict) and cfg.get("adapter"):
                adapter = get_adapter(cfg.get("adapter"))
                if adapter:
                    return adapter
                adapter = get_adapter(str(cfg.get("adapter")).split(".")[-1])
                if adapter:
                    return adapter
        except Exception:
            pass
        try:
            return get_adapter(provider)
        except Exception:
            return None

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

    def create_ticket(self, provider: str, intent: Dict[str, Any], dry_run: bool = False, timeout: Optional[int] = None, headless: Optional[bool] = None, slow_mo: Optional[int] = None, user_data_dir: Optional[str] = None) -> Dict[str, Any]:
        self._run_id = uuid.uuid4().hex
        provider = (provider or "").lower()
        cfg = self.provider_configs.get(provider, {}) if isinstance(self.provider_configs, dict) else {}
        base_url = self._prepare_base_url(cfg)
        timeout = timeout or DEFAULT_TIMEOUT

        def _result(status: str, detail: Optional[Dict[str, Any]] = None):
            out = {"status": status, "run_id": self._run_id}
            if detail:
                out.update(detail)
            return out

        if dry_run:
            steps = intent.get("steps") or []
            return _result("dry-run", {"steps": steps})

        try:
            with sync_playwright() as p:
                ctx_or_browser, page, is_persistent = self._launch(p)

                try:
                    if base_url:
                        try:
                            page.goto(base_url, timeout=timeout)
                        except Exception:
                            logger.debug("Navigation to base_url failed; continuing", exc_info=True)

                    try:
                        if cfg and cfg.get("login"):
                            sso_detected = self._detect_sso_on_page(page)
                            if sso_detected and not is_persistent:
                                wait_ms = SSO_MANUAL_WAIT_MS
                                self._log_step("sso_detected", {"provider": provider, "wait_ms": wait_ms})
                                try:
                                    page.wait_for_timeout(wait_ms)
                                except Exception:
                                    pass
                                logged_in = False
                                for sel in (cfg.get("open_new_selectors") or [])[:3]:
                                    try:
                                        if page.query_selector(sel):
                                            logged_in = True
                                            break
                                    except Exception:
                                        continue
                                if not logged_in:
                                    self._log_step("sso_manual_timeout", {"provider": provider})
                                    adapter = self._resolve_adapter(provider, cfg)
                                    if adapter:
                                        try:
                                            adapter_res = adapter.create_ticket(intent)
                                            if not _DEBUG_RETURN_RAW and isinstance(adapter_res, dict) and "raw" in adapter_res:
                                                adapter_res.pop("raw", None)
                                            return {provider: adapter_res}
                                        except Exception:
                                            logger.debug("Adapter fallback failed for provider %s: %s", provider, traceback.format_exc())
                                            return {provider: {"status": "error", "error": "adapter fallback failed"}}
                                    else:
                                        return _result("error", {"error": f"SSO required and no adapter for provider {provider}"})
                            else:
                                did_login = self._attempt_login(page, provider, cfg, timeout=8000)
                                if did_login:
                                    self._log_step("login_attempted", {"provider": provider})
                                else:
                                    # login failed or not confirmed; attempt adapter fallback
                                    self._log_step("login_failed_or_not_confirmed", {"provider": provider})
                                    adapter = self._resolve_adapter(provider, cfg)
                                    if adapter:
                                        try:
                                            adapter_res = adapter.create_ticket(intent)
                                            if not _DEBUG_RETURN_RAW and isinstance(adapter_res, dict) and "raw" in adapter_res:
                                                adapter_res.pop("raw", None)
                                            return {provider: adapter_res}
                                        except Exception:
                                            logger.debug("Adapter fallback failed for provider %s: %s", provider, traceback.format_exc())
                                            return {provider: {"status": "error", "error": "adapter fallback failed"}}
                                    else:
                                        return _result("error", {"error": f"login failed and no adapter for provider {provider}"})
                    except Exception:
                        logger.debug("Login attempt encountered an exception", exc_info=True)

                    steps = intent.get("steps")
                    if not steps:
                        inferred = self.infer_steps_with_llm(page, intent)
                        if inferred:
                            steps = inferred.get("steps", [])
                            if inferred.get("fields"):
                                intent.setdefault("fields", {}).update(inferred.get("fields", {}))

                    if not steps:
                        logger.warning("No UI steps available for provider %s; falling back to API adapter", provider)
                        adapter = self._resolve_adapter(provider, cfg)
                        if adapter:
                            try:
                                adapter_res = adapter.create_ticket(intent)
                                if not _DEBUG_RETURN_RAW and isinstance(adapter_res, dict) and "raw" in adapter_res:
                                    adapter_res.pop("raw", None)
                                return {provider: adapter_res}
                            except Exception:
                                logger.debug("Adapter fallback failed for provider %s: %s", provider, traceback.format_exc())
                                return {provider: {"status": "error", "error": "adapter fallback failed"}}
                        else:
                            return _result("error", {"error": f"no steps and no adapter for provider {provider}"})

                    ok = self.execute_steps(page, steps, intent)

                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

                    try:
                        submit_ok, submit_used = self._attempt_submit(page, cfg)
                        if submit_ok:
                            ok = True
                            self._log_step("submit_fallback_success", {"selector_used": submit_used})
                    except Exception:
                        logger.debug("submit fallback failed", exc_info=True)

                    try:
                        self.save_diagnostic(page, provider)
                    except Exception:
                        logger.debug("save_diagnostic_failed", exc_info=True)
                    try:
                        _save_selector_stats(self.selector_stats)
                    except Exception:
                        logger.debug("saving selector stats failed", exc_info=True)

                    return _result("ok" if ok else "failed", {"executed_steps": len(steps)})
                finally:
                    try:
                        self._close(ctx_or_browser)
                    except Exception:
                        logger.debug("Error during browser close", exc_info=True)
        except Exception:
            logger.exception("Unhandled exception in create_ticket")
            try:
                adapter = self._resolve_adapter(provider, cfg)
                if adapter:
                    try:
                        adapter_res = adapter.create_ticket(intent)
                        if not _DEBUG_RETURN_RAW and isinstance(adapter_res, dict) and "raw" in adapter_res:
                            adapter_res.pop("raw", None)
                        return {provider: adapter_res}
                    except Exception:
                        logger.debug("adapter fallback also failed", exc_info=True)
            except Exception:
                logger.debug("adapter fallback also failed", exc_info=True)
            return {"status": "error", "error": "unhandled exception during UI run"}
