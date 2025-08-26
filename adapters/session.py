# adapters/session.py
import os
import logging
import requests

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
try:
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter

    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"]),
        backoff_factor=0.5,
    )
    _SESSION.mount("https://", HTTPAdapter(max_retries=retry_strategy))
    _SESSION.mount("http://", HTTPAdapter(max_retries=retry_strategy))
except Exception:
    logger.debug("urllib3 Retry not available; continuing without advanced retry")

DEFAULT_REQUEST_TIMEOUT = int(os.getenv("ADAPTER_REQUEST_TIMEOUT_S", "30"))
DEBUG_RETURN_RAW = os.getenv("DEBUG_RETURN_RAW_API_RESPONSES", "false").strip().lower() in ("1", "true", "yes")


def session():
    """Return a requests.Session() instance (singleton). Tests can monkeypatch adapters.session.session."""
    return _SESSION
