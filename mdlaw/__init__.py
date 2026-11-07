"""
mdlaw — blazing-fast MyDramaList API wrapper.

Sources data from the official MDL Android app API (app-api.mydramalist.com/v1),
reconstructed from reversing com.mydramalist.app v2.3.18.

Run:  uvicorn mdlaw:app --port 8000
Self-check:  python -m mdlaw self   (offline)

Modules: _config (constants, headers, logger), caching (cache backends),
client (upstream client, auth, browse filters), server (FastAPI app and
routes), library (MDL), cli (command line).

Settings that change at runtime (TRANSPORT, MDL_USERNAME, MDL_PASSWORD,
RETRIES, ...) are not re-exported here: read or set them on their module,
e.g. ``mdlaw.client.TRANSPORT``.
"""
from ._config import API_BASE, API_KEY, __version__, default_headers, log
from .caching import CacheBackend, SQLCache, TTLCache, cache
from .client import (
    BROWSE_COUNTRIES,
    BROWSE_SORTS,
    BROWSE_TYPES,
    auth_enabled,
    auth_headers,
    browse_path,
    close_client,
    fetch,
    get_client,
    login,
    refresh,
)
from .server import app, lifespan
from .library import MDL
from .cli import main, self_check

__all__ = [
    "API_BASE", "API_KEY", "__version__", "default_headers", "log",
    "CacheBackend", "SQLCache", "TTLCache", "cache",
    "BROWSE_COUNTRIES", "BROWSE_SORTS", "BROWSE_TYPES",
    "auth_enabled", "auth_headers", "browse_path", "close_client", "fetch",
    "get_client", "login", "refresh",
    "app", "lifespan", "MDL", "main", "self_check",
]
