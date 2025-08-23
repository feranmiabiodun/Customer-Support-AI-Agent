# humaein_screening_scenario2
A config-driven Generic UI Agent that turns natural-language instructions into DOM interaction steps (mocked or LLM-inferred) and executes them with Playwright across multiple service providers (Zendesk, Freshdesk), with PII-redacted logs and API fallbacks.
# Generic UI Agent — Screening (Scenario 2)

A prototype **universal web action agent** that accepts a natural-language instruction (e.g. “Create a high priority ticket about login issues for [bob@example.com](mailto:bob@example.com)”), interprets it with an LLM (or deterministic mock), produces DOM interaction steps, and executes them with Playwright across multiple providers (Zendesk, Freshdesk). Designed to be config-driven, robust, and auditable.

---

## Highlights / goals accomplished

* Parse natural-language instructions → canonical JSON (`parser.py`).
* LLM (or mock) produces DOM `fields` + `steps` describing UI actions.
* `agents/generic_ui_agent.py` executes steps with Playwright (robust `safe_fill` / `safe_click`, iframe handling, label fallbacks).
* Provider configs are loaded from `providers/*.json` (or fall back to inline defaults for Zendesk & Freshdesk).
* API adapters available for fallback (`adapters/__init__.py`): Zendesk & Freshdesk REST APIs.
* Step-level, PII-redacted JSON-lines logs and selector success stats (`./ui_agent_diag/`).
* CLI `run_create_ticket.py` with `ui` (default) and `api` modes and `dry-run` flag.
* Backwards-compatible `agents/ui_agent.py` shim that uses `GenericUIAgent`.

---

# Quick start (development / local)

> Assumes you are at the project root (the folder containing `run_create_ticket.py`, `parser.py`, `agents/`, etc.).

1. Create & activate a virtual environment (recommended)

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

2. Install dependencies

```bash
pip install -r requirements.txt
# then install Playwright browsers (if running UI flows)
playwright install
```

3. Environment variables
   Create a `.env` file or export in your shell. Minimum useful set for dry-run / tests:

```env
# LLM (for live inference)
GROQ_API_KEY=your_groq_api_key_here
GROQ_URL=https://api.groq.com/openai/v1/chat/completions
GROQ_MODEL=llama-3.3-70b-versatile

# Playwright / UI behavior (optional)
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

# Diagnostic directory (optional)
UI_AGENT_DIAG_DIR=./ui_agent_diag
```

> If you only want to run dry-run and tests, you do not need working provider credentials.

---

# Run examples

**Dry-run (no browser calls) — recommended for testing**

```bash
python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com" ui dry-run
```

**UI mode (Playwright, will act on provider UIs)**

```bash
python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com" ui
```

**API-only mode (skips browser, uses REST adapters)**

```bash
python run_create_ticket.py "Create a low priority ticket about password reset for alice@example.com" api
```

---

# Files & layout (important files)

```
.
├─ parser.py                 # LLM prompt → JSON parse, normalization, attaches mock steps (if enabled)
├─ run_create_ticket.py      # CLI entrypoint (ui/api/dry-run)
├─ schema.json               # JSON schema used by parser to validate LLM output
├─ prompt_template.txt       # prompt template for LLM (used by parser)
├─ requirements.txt
├─ README.md
├─ providers/                # optional folder with provider JSON (zendesk.json, freshdesk.json)
├─ agents/
│  ├─ __init__.py
│  ├─ generic_ui_agent.py    # core engine (LLM inference, execute_steps, logging, selector stats)
│  └─ ui_agent.py            # thin backward-compatible shim
├─ adapters/
│  └─ __init__.py            # Zendesk + Freshdesk REST API adapters
├─ llm/
│  └─ mock_dom_steps.py      # deterministic mock DOM steps (used when USE_MOCK_DOM=true)
└─ ui_agent_diag/            # generated: steps.jsonl, selector_stats.json, screenshots/.html
```

---

# Architecture summary

* **parser.py**

  * Renders the prompt from `prompt_template.txt` and calls the LLM (`call_groq`).
  * Extracts JSON from the LLM response, validates against `schema.json`.
  * Applies tolerant normalization (coerce `requester`, default providers, auto-fill `description`).
  * Optionally attaches mock DOM steps from `llm/mock_dom_steps.py` when `USE_MOCK_DOM` is true.
  * Adds `meta.instruction` so the agent can include the original instruction in LLM prompts.

* **GenericUIAgent (`agents/generic_ui_agent.py`)**

  * Loads provider configs from `providers/*.json` or uses inline defaults.
  * When `intent["steps"]` is missing, captures a redacted DOM snippet and asks the LLM to infer `fields` + `steps`.
  * Executes steps with robust `safe_fill` and `safe_click`. Uses selector success statistics to prefer more reliable selectors.
  * Persists redacted, structured logs per step (`steps.jsonl`) and `selector_stats.json`.
  * Saves diagnostics (screenshots / HTML) when needed and uses API adapter fallback on SSO/login failures.

* **Adapters**

  * Simple wrappers to call Zendesk/Freshdesk REST APIs as fallback.

---

# Logs, diagnostics, and privacy

* Logs & diagnostics folder: `./ui_agent_diag/` (default; set via `UI_AGENT_DIAG_DIR`).

  * `steps.jsonl` — JSON-lines with per-step events (redacted).
  * `selector_stats.json` — aggregated selector tries/successes used to rank selectors in future runs.
  * `*.png` and `*.html` — screenshots / page HTML (HTML is minimally redacted for emails).

* **PII redaction**: email addresses and obvious tokens are redacted in logs and console outputs. Still treat any saved diagnostics carefully.

---

# Tests

A small pytest suite is provided (in `tests/`) to assert imports and dry-run behavior. Run tests with:

```bash
pytest -q
```

Tests are designed to avoid network/LLM/Playwright by mocking `parser.parse_instruction` or using `dry-run`.

---

# Troubleshooting & common gotchas

* **`ModuleNotFoundError: No module named 'generic_ui_agent'`**

  * You must import via package path: `from agents import generic_ui_agent` or `from agents.generic_ui_agent import GenericUIAgent`.
  * Run commands from the project root (where `parser.py` sits) so Python finds local packages.

* **Syntax errors after copy/paste**

  * Ensure indentation is consistent and you're using UTF-8 encoding. Use the exact code blocks provided.

* **LLM calls fail**

  * Check `GROQ_API_KEY` and `GROQ_URL`. If network-restricted, use `USE_MOCK_DOM=true` (default) to attach mock steps and run dry-runs.

* **Schema validation fails**

  * `parser.parse_instruction` validates against `schema.json`. If your schema is strict, either:

    * update `schema.json` to allow `meta` or optional fields, or
    * remove/adjust the `meta` attachment in `parser.py`.

* **Playwright errors**

  * Make sure `playwright install` was executed to download browser binaries.
  * For visual debugging set `PLAYWRIGHT_HEADLESS=false`.

---

# How to add a new provider

1. Create `providers/<provider_name>.json` with keys:

   * `base_url` (optionally with `{ENV_VAR}` placeholders)
   * `login` selectors (optional)
   * `open_new_selectors` — list of selectors to open the ticket form
   * `fields` — mapping of logical fields to selector\_candidates (list)
   * `submit_selectors` — selectors to submit the form

2. The generic agent will automatically load `providers/*.json` on startup and prefer file-based configs.

---

# Example workflow (developer)

1. Parse & dry-run:

```bash
python run_create_ticket.py "Create a high priority ticket about login issues for bob@example.com" ui dry-run
```

2. Inspect `ui_agent_diag/steps.jsonl` to see redacted sequence and run\_id.
3. Run a real UI flow (after Playwright install and credentials):

```bash
python run_create_ticket.py "Create..." ui
```
