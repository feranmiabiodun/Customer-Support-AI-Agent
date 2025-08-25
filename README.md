# Universal UI Agent

## Overview

A prototype **universal web action agent** that accepts a single natural-language instruction (for example:
`Create a high priority ticket about login issues for bob@example.com`) and executes that goal across multiple web frontends with differing UIs. The system combines a natural-language parser, optional LLM-based DOM inference, robust Playwright browser automation, and provider API adapters to reliably perform a single goal (submit a support ticket) across provider implementations such as Zendesk and Freshdesk.

This README describes the project's architecture, components, configuration placeholders, runtime flow, logging/diagnostics, testing approach, extensibility points, and known limitations.

---

## Goals & Requirements (mapping)

The project implements the screening scenario requirements:

* Accepts a natural-language instruction and converts it into a canonical JSON.
* Uses an LLM (or deterministic mock) to interpret instructions and optionally infer DOM interaction steps.
* Uses Playwright to automate browsers: navigation, authentication, and DOM interactions.
* Supports at least two providers (Zendesk, Freshdesk) with differing DOM structures via `providers/*.json`.
* Abstracts provider-specific logic behind provider configs and a pluggable adapter interface.
* Provides structured logging and diagnostics and includes basic recoverability heuristics (selector scoring, label fallbacks, multi-strategy submit).
* Includes passcode (2FA) retrieval by IMAP and a browser-inbox fallback.
* Offers a mock mode for deterministic CI/local testing (attaches deterministic DOM steps).

Stretch features implemented or scaffolded:

* IMAP passcode retrieval and browser-inbox fallback.
* Selector scoring and persistence (`selector_stats.json`).
* OCR hook (optional, `pytesseract`) for augmenting LLM prompts.
* Adapter registry for pluggable API fallbacks.
* Logging of step-level events (JSON-lines).

---

## Architecture (high-level)

* **Parser** (`parser.py` + `prompt_template.txt`, `schema.json`): Turns an NL instruction into a canonical JSON instruction object (subject, description, requester, priority, providers, metadata). Supports deterministic mock DOM step attachment (`llm/mock_dom_steps.py`) for testability.
* **Agent core** (`agents/generic_ui_agent.py`): Core engine that:

  * Loads provider configs from `providers/*.json`.
  * Launches Playwright browser or persistent context.
  * Attempts login flows; detects passcode prompt and retrieves passcode via IMAP or inbox scraping.
  * Executes DOM steps using robust primitives (`safe_fill`, `safe_click`) with retries and fallbacks.
  * Attempts submission with multi-strategy `_attempt_submit`.
  * Persists redacted structured logs and diagnostics.
  * Falls back to adapters when UI flow is blocked (SSO, missing fields).
* **Shim** (`agents/ui_agent.py`): Thin compatibility layer providing the same public functions existing callers expect.
* **Adapters** (`adapters/__init__.py`): Provider API implementations (Zendesk, Freshdesk) and a registry (`get_adapter`) for fallback.
* **Providers** (`providers/*.json`): JSON configuration files describing base URLs, selectors for login/open/new/fields/submit, optional passcode selectors and inbox details.
* **LLM mock** (`llm/mock_dom_steps.py`): Deterministic mapping from a parsed instruction to a set of DOM steps for reliable CI tests.
* **Runner** (`run_create_ticket.py`): CLI entrypoint that parses instructions and dispatches to UI automation or API mode (and supports dry-run).
* **Tests** (`tests/`): Smoke tests for imports and dry-run.

---

## Component descriptions

* `run_create_ticket.py` — CLI runner. Parses the instruction and dispatches to either UI automation or adapter API; supports dry-run mode.
* `parser.py` — Renders the prompt template, calls the LLM (Groq/OpenAI-compatible endpoint) or uses mock mode, extracts JSON, validates against `schema.json`, normalizes into canonical form.
* `prompt_template.txt` — LLM prompt template that requires a strict JSON-only response following the schema.
* `schema.json` — JSON Schema for the canonical instruction object.
* `agents/generic_ui_agent.py` — Primary automation engine. Implements login, passcode retrieval, DOM step execution, submit heuristics, diagnostics, and adapter fallbacks.
* `agents/ui_agent.py` — Backwards-compatible shim that instantiates `GenericUIAgent` and exposes `create_ticket()` convenience wrappers.
* `adapters/__init__.py` — Adapter implementations and registry (`get_adapter(provider_name)`).
* `providers/` — Per-provider JSON files describing base\_url, login selectors, field selector candidates, submit selectors, optional passcode selectors, etc.
* `llm/mock_dom_steps.py` — Generates deterministic DOM steps for tests and CI.
* `tests/` — Automated tests (e.g., dry-run behavior).

---

## Configuration placeholders

Use the following environment placeholders (replace values locally as appropriate):

```
# LLM (for live inference)
GROQ_API_KEY=your_groq_api_key_here
GROQ_URL=https://api.groq.com/openai/v1/chat/completions
GROQ_MODEL=llama-3.3-70b-versatile

# Playwright / UI behavior
PLAYWRIGHT_HEADLESS=true
PLAYWRIGHT_USER_DATA_DIR=

# Zendesk (UI + API)
ZENDESK_SUBDOMAIN=yoursubdomain
ZENDESK_EMAIL=agent@example.com
ZENDESK_PASSWORD=agent_password
ZENDESK_API_TOKEN=your_api_token

# Freshdesk (UI + API)
FRESHDESK_DOMAIN=yourdomain
FRESHDESK_EMAIL=agent@example.com
FRESHDESK_PASSWORD=agent_password
FRESHDESK_API_KEY=your_api_key

# IMAP (for passcode retrieval)
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_PORT=993
EMAIL_IMAP_USER=your_imap_username_here
EMAIL_IMAP_PASSWORD=your_imap_password_or_app_password_here
EMAIL_INBOX_URL=https://mail.google.com/mail/u/0/#inbox
EMAIL_PASSCODE_SUBJECT_REGEX=automation|passcode|verification
EMAIL_PASSCODE_REGEX=(\d{4,8})
EMAIL_IMAP_POLL_TIMEOUT_S=30
EMAIL_IMAP_POLL_INTERVAL_S=3.0

# Diagnostic directory (optional)
UI_AGENT_DIAG_DIR=./ui_agent_diag

# Toggle mock DOM behavior for parser to attach deterministic mock steps instead of calling the LLM
# Set to "true" to enable mock steps (useful for CI and local testing).
USE_MOCK_DOM=true

# Other debug toggles
UI_AGENT_SAVE_RAW_DIAGNOSTICS=false
DEBUG_RETURN_RAW_API_RESPONSES=false
UI_AGENT_USE_OCR=false
```

---

## Runtime flow (detailed)

1. **Parse instruction**

   * `parser.parse_instruction()` transforms natural language into canonical JSON. If `USE_MOCK_DOM=true`, mock DOM steps are attached for stable testing.

2. **Dispatch**

   * `run_create_ticket.dispatch(parsed, mode="ui"|"api", dry_run=bool)` calls either UI automation or adapters.

3. **UI automation**

   * `GenericUIAgent.create_ticket(provider, intent, dry_run=False)`:

     * Loads provider config (`providers/<provider>.json`) and formats base URL with env vars.
     * Launches Playwright browser or persistent profile when `PLAYWRIGHT_USER_DATA_DIR` is configured.
     * Navigates to `base_url`.
     * If `login` config exists:

       * Attempts auto-fill of login fields using configured selectors.
       * Clicks configured login/button selectors and waits for page load.
       * Detects passcode prompt via heuristics. If present:

         * Tries IMAP (poll mailbox for configured subject regex and extract numeric code).
         * If IMAP fails, tries browser-inbox scraping (open inbox URL and find message with subject regex).
         * Fills passcode input and clicks verify/submit selectors.
       * If SSO is detected and automation is blocked, uses manual-wait or adapter fallback based on config.
     * Uses provided `intent["steps"]` or invokes LLM-driven `infer_steps_with_llm()` (if `USE_MOCK_DOM=false`) to produce DOM actions.
     * Executes `steps` using `safe_fill` and `safe_click` with retries, label fallback, and selector scoring.
     * Attempts multi-strategy submit (`_attempt_submit`): configured submit selectors, JS click, keyboard `Ctrl+Enter`, `form.submit()`, and disabled-attribute workaround. Logs which strategy succeeded.
     * Saves redacted diagnostic HTML/PNG and updates selector stats.

4. **Adapter fallback**

   * If UI automation cannot proceed (SSO/manual block, missing required fields, or other fatal errors), the agent resolves an adapter via `get_adapter(provider)` and calls `adapter.create_ticket(parsed)`.

5. **Logging**

   * Step-level structured, redacted logs are emitted and persisted to `UI_AGENT_DIAG_DIR/steps.jsonl`.
   * Diagnostic HTML and PNG snapshots are written per run with timestamps to the same directory for visual inspection.
   * Selector statistics are tracked in `selector_stats.json` to improve selector ordering over time.

---

## LLM vs Mock behavior

* **Mock mode (`USE_MOCK_DOM=true`)**

  * Deterministic DOM steps are attached to parsed instructions via `llm/mock_dom_steps.py`. This mode provides stable, testable behavior for CI and local runs without calling an LLM.
* **LLM mode (`USE_MOCK_DOM=false`)**

  * The parser or `infer_steps_with_llm()` will call the configured Groq/OpenAI-compatible endpoint to produce fields/steps from a page snippet and instruction. The prompt expects strict JSON output and the system extracts and validates the JSON.

---

## Diagnostics & logs

* Structured step logs (redacted) are written to `UI_AGENT_DIAG_DIR/steps.jsonl`. Each entry includes: `run_id`, `ts`, `event`, and the `step` object.
* Diagnostic HTML and PNG snapshots are written per run with timestamps to the same directory for visual inspection.
* Selector success/attempt stats persist across runs in `selector_stats.json` to order candidate selectors adaptively.

---

## Extensibility

* **Add a new provider:** drop a `providers/<name>.json` file describing `base_url`, `login` selectors, `fields`, `open_new_selectors`, `submit_selectors`, and optional `passcode_*` entries. The agent uses provider configs to guide heuristics and LLM prompts.
* **Adapter plugin:** implement an adapter class that exposes `create_ticket(payload)` and register it in `adapters/__init__.py` with `get_adapter("<name>")`.
* **LLM improvements:** tune `prompt_template.txt`, increase validation in `parser.py`, and add confidence checks / ambiguity metadata.
* **Recovery heuristics:** improve DOM inference by adding vision-based selectors (screenshot OCR), DOM-tree embedding, or LLM prompt-chaining.

---

## Testing

The project has a deterministic test mode (mock DOM) to run tests that do not require network LLM calls or real UI interactions. The provided tests exercise importability and dry-run behavior; additional provider-specific unit and integration tests are recommended.

### Testing commands & examples (Windows `cmd` examples included)

**1) Create & activate Python virtual environment (Windows `cmd`)**

```
python -m venv .venv
.venv\Scripts\activate
```

**2) Install dependencies**
(assuming a `requirements.txt` exists with at least `playwright`, `requests`, `jsonschema`, `python-dotenv`, `pillow`, `pytesseract` as needed)

```
pip install -r requirements.txt
```

**3) Install Playwright browsers**

```
python -m playwright install
```

**4) Ensure environment variables**
Create a `.env` file in project root using the placeholders shown earlier. `python-dotenv` is used by `parser.py` if present, so the CLI will pick up variables from `.env`.

Windows `cmd` example to set a single env var for a session (temporary):

```
set PLAYWRIGHT_HEADLESS=false
set PLAYWRIGHT_USER_DATA_DIR=C:\path\to\playwright-profile
```

(Prefer `.env` for multi-line values and to avoid `cmd` quoting pitfalls.)

**5) Run unit/smoke tests**

```
python -m pytest -q
```

**6) Example CLI runs (Windows `cmd`)**

UI mode (real browser automation):

```
python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com" ui
```

UI mode, dry-run (shows steps without launching):

```
python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com" ui dry-run
```

API-only mode:

```
python run_create_ticket.py "Create a medium priority ticket about API 500 errors for alice@example.com" api
```

Run with mock DOM (CI-friendly) — set `USE_MOCK_DOM=true` in `.env` or environment:

```
set USE_MOCK_DOM=true
python run_create_ticket.py "Create a low priority ticket about password reset issues for alice@example.com" ui
```

**7) Debugging & diagnostics**

* Diagnostic snapshots and step logs are placed in `UI_AGENT_DIAG_DIR` (default `./ui_agent_diag`) — review the latest `zendesk_*.html` / `freshdesk_*.html` and `steps.jsonl`.
* If Playwright persistent context fails due to an improperly set `PLAYWRIGHT_USER_DATA_DIR`, check the value and ensure the directory path exists and does not contain stray quoting.

---

## Known limitations & reliability considerations

* Third-party SSO pages and some Google sign-in flows may actively block automated browsers. Persistent browser profiles (`PLAYWRIGHT_USER_DATA_DIR`) or adapter fallback remain the most reliable approaches for those cases.
* IMAP access requires credentials and, for providers like Gmail, an app password or properly configured account settings for automated IMAP access.
* DOM changes on provider pages can break selector heuristics; selector scoring and LLM-based inference mitigate but do not eliminate maintenance.
* OCR and inbox-scraping are best-effort; IMAP is preferred when available.

---

## Design decisions & rationale

* **Config-driven provider definitions** (JSON) avoid hardcoding provider logic and make the system extensible.
* **Mock DOM generator** enables deterministic CI tests and isolates parsing/automation logic from LLM variability.
* **Adapter registry** separates API fallback logic and permits reusing provider APIs when UI automation is blocked.
* **Selector scoring persistence** allows the system to learn which selectors work most reliably in the particular environment over time.
* **Multi-strategy submit** increases success rate across divergent UIs and cases where the visible submit button is not a standard form button.

---

## Contact / maintenance notes

* The codebase centralizes provider-specific behavior in `providers/*.json` and adapters; to accommodate UI drift, add or update provider configs and expand the adapter registry.
* The LLM prompt template and schema provide a contract for the parser output—any changes to the schema should be accompanied by prompt updates.