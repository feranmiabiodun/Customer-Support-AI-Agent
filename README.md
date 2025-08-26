# Universal UI Agent - Abiodun Adeagbo's submission

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

Stretch features implemented:

* IMAP passcode retrieval and browser-inbox fallback.
* Selector scoring and persistence.
* OCR hook (`pytesseract`) for augmenting LLM prompts.
* Adapter registry for pluggable API fallbacks.
* Logging of step-level events (JSON-lines).

---

## Architecture (high-level)

```

├─ run_create_ticket.py
├─ parser.py
├─ agents
│  ├─ __init__.py
│  ├─ ui_agent.py
│  └─ compat_ui_shim.py
├─ adapters
│  ├─ __init__.py
│  ├─ freshdesk.py
│  ├─ zendesk.py
│  └─ session.py
├─ llm
│  ├─ __init__.py
│  └─ mock_dom_steps.py
├─ tests
│  ├─ test_imports_and_dryrun.py
│  └─ test_adapters.py
├─ schema.json
├─ prompt_template.txt
├─ providers
│  ├─ zendesk.json
│  └─ freshdesk.json
├─ requirements.txt
└─ README.md

```

---

## Flow-oriented file explanations

### `run_create_ticket.py` — Entrypoint (CLI + programmatic dispatcher)

This is the canonical entrypoint for the project. Callers (humans or CI) invoke this to run the end-to-end flow. It: accepts a natural-language instruction, calls the parser to get a canonical `parsed` intent, and dispatches that intent to one or more providers via either the UI automation path or the API adapters. Use this script for production-style runs and for the simple, reviewer-friendly end-to-end invocation.

**CLI examples (canonical ways to exercise the end-to-end flow):**

```bash
# UI mode (the browser automation agent; requires credentials).
python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com" ui

# UI dry-run (no browser calls)
python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com" ui dry-run

# API mode (fall back or skip browser; call adapters)
python run_create_ticket.py "Create a low priority ticket about password reset for bob@example.com" api
```

---

### `parser.py` — Instruction → canonical parsed intent

`run_create_ticket.py` calls this module to convert free text into a validated, normalized JSON intent the rest of the system understands. The parser enforces schema constraints, normalizes requester/provider shapes, and (when configured) attaches deterministic mock DOM steps so the agent can run offline.

---

### `agents/ui_agent.py` — The actor that performs UI automation

This is the Playwright-powered agent that, given a canonical parsed intent, attempts to execute the UI flow in a browser. It is the central automation engine (selector heuristics, robust fill/click primitives, diagnostics, selector stats, SSO detection, and API fallback triggers). In the system flow it is invoked by the dispatcher when the UI mode is selected.

---

### `agents/compat_ui_shim.py` — Backwards-compatible shim

A thin wrapper that instantiates the `CoreAgent` and provides the stable functions existing call sites expect. It exists to keep the public API tidy and to make `run_create_ticket.py` calls concise.

---

### `adapters` — provider API modules

The `adapters` directory contains three explicit modules:

* `adapters/freshdesk.py` — Freshdesk API adapter. Implements `FreshdeskAdapter` with a `create_ticket(payload)` method that uses the shared HTTP session helpers in `adapters/session.py`. Reads `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY` from environment (supports refreshing config if `.env` is loaded at runtime). Returns normalized responses like `{"status":"ok","ticket_id": ...}` or `{"status":"error", "code": ..., "body": ...}`.

* `adapters/zendesk.py` — Zendesk API adapter. Implements `ZendeskAdapter` with a `create_ticket(payload)` method that uses `adapters/session.py` for HTTP with retries. Reads `ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`, and `ZENDESK_API_TOKEN` from environment and returns normalized responses like the Freshdesk adapter.

* `adapters/session.py` — Shared `requests.Session` and HTTP helpers. Defines `session()` (returns a `requests.Session` with optional `urllib3` Retry), `DEFAULT_REQUEST_TIMEOUT`, and `DEBUG_RETURN_RAW` flags used by both adapters.

---

### `llm/mock_dom_steps.py` — Deterministic mock DOM steps (offline mode)

Generates predictable `fields` + `steps` that the agent can execute without an LLM or access to a live page. The parser attaches these in mock mode so the entire dispatch → agent → execution flow can be exercised offlin and deterministically (useful for reviewers and tests). Run with mock DOM (CI-friendly) — set `USE_MOCK_DOM=true` in `.env` or environment:

```
set USE_MOCK_DOM=true
python run_create_ticket.py "Create a low priority ticket about password reset issues for bob@example.com" ui
```

---

### `tests/test_imports_and_dryrun.py` — Test harness and why it is included

This test file provides a fast, offline verification that:

* the codebase imports cleanly (smoke test), and
* the programmatic dry-run dispatch path behaves as expected.

Its importance: a single, reproducible command (`pytest -q`) shows that the submission runs and that the dry-run behavior matches the intended contract (no network or browser required). To run just the dry-run smoke test:

```bash
pytest tests/test_imports_and_dryrun.py::test_run_create_ticket_dry_run -q
```

---

### `tests/test_adapters.py` — Adapter-level tests

This file contains unit tests for `FreshdeskAdapter` and `ZendeskAdapter`, verifying that they correctly handle environment configuration, priority/status mapping, successful ticket creation (with monkeypatched responses), and error conditions, ensuring consistent and predictable behavior without making real API calls.

---

## Supporting artifacts (schema, prompt, providers, config & docs)

* `schema.json`
  Declares the canonical JSON contract used by the parser to validate LLM output.

* `prompt_template.txt`
  The LLM prompt used by the parser. Included in order to understand how instructions are framed for machine interpretation.

* `providers/` (`zendesk.json`, `freshdesk.json`)
  Per-provider configuration (selectors, URLs, submit selectors). Included so the automation logic can be configured without code changes and for provider-specific mappings.

* `requirements.txt`
  Dependency list to install the environment for running tests and (optionally) Playwright and OCR components.

---

## Why the files are kept modular (why not a single merged script)

* **Separation of concerns:** parsing, UI execution, API adapters, LLM mocks, and the CLI dispatcher are distinct responsibilities — making each file focused and easy to review for its role.
* **Testability:** unit tests can import and patch single modules (e.g., parser) without running the whole system; this enables deterministic offline tests for graders.
* **Maintainability & extensibility:** provider configs live in `providers` so adding a new service requires only a JSON file, not code restructuring.
* **Reviewer ergonomics:** a single `run_create_ticket.py` entrypoint gives a low-friction path; the rest of the codebase is visible and organized so a grader can inspect architecture and tradeoffs quickly.

---

## Configuration placeholders

Use the following environment placeholders in an .env file (replace values locally as appropriate):

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

## Diagnostics & logs

* Structured step logs (redacted) are written to `UI_AGENT_DIAG_DIR/steps.jsonl`. Each entry includes: `run_id`, `ts`, `event`, and the `step` object.
* Diagnostic HTML and PNG snapshots are written per run with timestamps to the same directory for visual inspection.

---

## Extensibility

* **Add a new provider:** drop a `providers/<name>.json` file describing `base_url`, `login` selectors, `fields`, `open_new_selectors`, `submit_selectors`, and optional `passcode_*` entries. The agent uses provider configs to guide heuristics and LLM prompts.
* **Adapter plugin:** implement an adapter class that exposes `create_ticket(payload)` and register it in `adapters__init__.py` with `get_adapter("<name>")`.
* **LLM improvements:** tune `prompt_template.txt`, increase validation in `parser.py`, and add confidence checks / ambiguity metadata.
* **Recovery heuristics:** improve DOM inference by adding vision-based selectors (screenshot OCR), DOM-tree embedding, or LLM prompt-chaining.

---

**7) Debugging & diagnostics**

* Diagnostic snapshots and step logs are placed in `UI_AGENT_DIAG_DIR` (default `ui_agent_diag`) — review the latest `zendesk_*.html` / `freshdesk_*.html` and `steps.jsonl`.
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

### Thank you.
