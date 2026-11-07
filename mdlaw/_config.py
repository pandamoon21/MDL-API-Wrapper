"""Shared configuration: upstream base URL, client nonce, version, logger,
header scheme and small helpers used by every other module."""
from __future__ import annotations

import logging
import os
import secrets
import string
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_BASE = "https://app-api.mydramalist.com/v1"

# `mdl-api-key` is NOT a secret: the MDL app generates a random 20-char
# nonce per launch and the server never validates it (verified live: no
# header and random values both return 200). It is only a client nonce, so
# we generate one too. You can still pin a specific value with MDL_API_KEY
# for reproducible requests — it carries no security meaning.
API_KEY = os.environ.get("MDL_API_KEY", "").strip() or "".join(
    secrets.choice(string.ascii_letters + string.digits) for _ in range(20)
)

__version__ = "1.9.0"

# Library code only logs to this logger. The CLI (-v / -vv) and the server
# (MDL_LOG_LEVEL) attach handlers; library users configure it themselves.
# Never log tokens or passwords.
log = logging.getLogger("mdlaw")

# Header scheme: without User-Agent + Accept-Language + Accept the API
# returns 403 (Cloudflare WAF).
def default_headers(token: str | None = None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "mdl-api-key": API_KEY,
        "device": "android",
        "X-Client-Platform": "mobile",
        "X-Client-App": "mdl_flutter",
        "X-Client-OS": "android",
        "X-Client-OS-Version": "14",
        "X-Client-Device-Model": "sdk_gphone64_arm64",
        "X-App-Version": "2.3.18",
        "Accept-Language": "en",
        "User-Agent": "okhttp/4.12.0",
        "Accept": "*/*",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _qs(path: str, **params: object) -> str:
    """Upstream path with URL-encoded query params (order preserved), so user
    input like ``q="a&page=9"`` can't inject params or split the cache key."""
    return f"{path}?{urlencode(params)}" if params else path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
