#!/usr/bin/env python3
"""
mdlaw — blazing-fast MyDramaList API wrapper.

Sources data from the official MDL Android app API (app-api.mydramalist.com/v1),
reconstructed from reversing com.mydramalist.app v2.3.18.

Run:  uvicorn mdlaw:app --port 8000
Self-check:  python mdlaw.py self   (offline)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import string
import time
import uuid

import httpx
from typing import Any
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# Config / obfuscation
# ---------------------------------------------------------------------------
API_BASE = "https://app-api.mydramalist.com/v1"

# `mdl-api-key` is NOT a secret: the MDL app generates a random 20-char
# nonce per launch and the server never validates it (verified live: no
# header and random values both return 200). It is only a client nonce, so
# we generate one too. You can still pin a specific value with MDL_API_KEY
# for reproducible requests — it carries no security meaning.
# (Confirmed by danieyal/pymdl docs/api-key-extraction.md.)
API_KEY = os.environ.get("MDL_API_KEY", "").strip() or "".join(
    secrets.choice(string.ascii_letters + string.digits) for _ in range(20)
)

# Optional: use curl_cffi (browser/mobile TLS impersonation) instead of httpx.
# The real gate on the MDL API is Cloudflare bot protection, which
# fingerprints the TLS/HTTP2 handshake (JA3/JA4). From most residential IPs a
# plain httpx client with the okhttp header scheme passes; from datacenter /
# flagged IPs Cloudflare serves a "403 Just a moment..." challenge to plain
# TLS stacks. Set MDL_TRANSPORT=curl_cffi to use curl_cffi's impersonation
# (e.g. safari_ios) which passes the challenge.
# Transport. Default is curl_cffi (browser/mobile TLS impersonation), which
# passes Cloudflare's JA3/JA4 bot protection from flagged/datacenter IPs.
# Set MDL_TRANSPORT=httpx to use a plain httpx TLS stack (lighter, no
# curl_cffi dependency) — works from most residential IPs.
TRANSPORT = os.environ.get("MDL_TRANSPORT", "curl_cffi").strip().lower()

__version__ = "1.5.0"

# Header scheme recovered from RequestHeaders.json() — validated: without
# User-Agent + Accept-Language + Accept the API returns 403 (Cloudflare WAF).
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


# ---------------------------------------------------------------------------
# Cache backends — in-memory (default) or SQL DB (sqlite/mysql/postgres)
# ---------------------------------------------------------------------------
# Pick backend via env:
#   MDL_CACHE_BACKEND=memory|sqlite|mysql|postgres   (default: memory)
#   MDL_CACHE_DB_URL=sqlite:///mdlaw_cache.db | mysql://u:p@host/db |
#                    postgresql://u:p@host/db
# Response change detection: every stored response keeps a sha256 hash of its
# JSON. On TTL expiry the upstream is re-fetched; if the hash differs the row
# is updated with changed=1 + updated_at, otherwise changed=0 and TTL extends.
# There is no real-time push from MDL — TTL + hash comparison is the pattern.
class CacheBackend:
    hits = 0
    misses = 0

    def get(self, key: str) -> object | None:  # pragma: no cover
        raise NotImplementedError

    def put(self, key: str, value: object, ttl: float) -> None:  # pragma: no cover
        raise NotImplementedError

    def entries(self) -> int:  # pragma: no cover
        raise NotImplementedError

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "backend": type(self).__name__,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else None,
            "entries": self.entries(),
        }


class TTLCache(CacheBackend):
    def __init__(self) -> None:
        self._d: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        item = self._d.get(key)
        if item is None:
            self.misses += 1
            return None
        expires, value = item
        if time.monotonic() > expires:
            self._d.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: object, ttl: float) -> None:
        self._d[key] = (time.monotonic() + ttl, value)

    def entries(self) -> int:
        return len(self._d)


def _json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SQLCache(CacheBackend):
    """Persistent cache backed by sqlite (stdlib) / mysql (pymysql) / postgres (psycopg)."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS mdlaw_cache (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        expires_at REAL NOT NULL,
        hash       TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        changed    INTEGER NOT NULL DEFAULT 0
    )"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn = None
        self._flavor = ("mysql" if url.startswith("mysql://")
                        else "postgres" if url.startswith(("postgresql://", "postgres://"))
                        else "sqlite")
        self._ph = "%s" if self._flavor != "sqlite" else "?"

    # -- connection --------------------------------------------------------
    def _connect(self):
        if self._flavor == "sqlite":
            import sqlite3
            path = self.url[len("sqlite:///"):] or ":memory:"
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        if self._flavor == "mysql":
            try:
                import pymysql
            except ImportError:
                raise RuntimeError("MySQL cache backend requires: pip install pymysql")
            from urllib.parse import urlsplit
            u = urlsplit(self.url)
            return pymysql.connect(
                host=u.hostname or "localhost", port=u.port or 3306,
                user=u.username or "", password=u.password or "",
                database=u.path.lstrip("/") or "", autocommit=False)
        # postgres
        try:
            import psycopg
        except ImportError:
            raise RuntimeError("PostgreSQL cache backend requires: pip install 'psycopg[binary]'")
        return psycopg.connect(self.url)

    def _cursor(self):
        if self._conn is None:
            self._conn = self._connect()
            self._conn.execute(self._SCHEMA)
            self._conn.commit()
        try:
            return self._conn.cursor()
        except Exception:
            # server went away — reconnect once
            self._conn = self._connect()
            self._conn.execute(self._SCHEMA)
            self._conn.commit()
            return self._conn.cursor()

    # -- ops ---------------------------------------------------------------
    def get(self, key: str) -> object | None:
        cur = self._cursor()
        cur.execute(f"SELECT value, expires_at FROM mdlaw_cache WHERE key = {self._ph}", (key,))
        row = cur.fetchone()
        cur.close()
        if row is None:
            self.misses += 1
            return None
        value, expires = row
        if expires <= time.time():
            self._delete(key)
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(value)

    def _delete(self, key: str) -> None:
        cur = self._cursor()
        cur.execute(f"DELETE FROM mdlaw_cache WHERE key = {self._ph}", (key,))
        self._conn.commit()
        cur.close()

    def put(self, key: str, value: object, ttl: float) -> None:
        h = _json_hash(value)
        now = time.time()
        expires = now + ttl
        cur = self._cursor()
        # detect change: compare hash of stored row vs new one
        cur.execute(f"SELECT hash, created_at FROM mdlaw_cache WHERE key = {self._ph}", (key,))
        row = cur.fetchone()
        if row is None:
            created = updated = now
            changed = 0
        else:
            old_hash, created = row
            changed = 0 if old_hash == h else 1
            updated = now
        value_json = json.dumps(value, ensure_ascii=False)
        if self._flavor == "mysql":
            sql = (f"INSERT INTO mdlaw_cache (key,value,expires_at,hash,created_at,updated_at,changed) "
                   f"VALUES (%s,%s,%s,%s,%s,%s,%s) "
                   f"ON DUPLICATE KEY UPDATE value=VALUES(value), expires_at=VALUES(expires_at), "
                   f"hash=VALUES(hash), updated_at=VALUES(updated_at), changed=VALUES(changed)")
        else:
            sql = (f"INSERT INTO mdlaw_cache (key,value,expires_at,hash,created_at,updated_at,changed) "
                   f"VALUES ({self._ph},{self._ph},{self._ph},{self._ph},{self._ph},{self._ph},{self._ph}) "
                   f"ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at, "
                   f"hash=excluded.hash, updated_at=excluded.updated_at, changed=excluded.changed")
        cur.execute(sql, (key, value_json, expires, h, created, updated, changed))
        self._conn.commit()
        cur.close()

    def entries(self) -> int:
        cur = self._cursor()
        cur.execute("SELECT COUNT(*) FROM mdlaw_cache")
        n = cur.fetchone()[0]
        cur.close()
        return n

    def stats(self) -> dict:
        s = super().stats()
        s["backend"] = f"SQLCache({self._flavor})"
        return s


def _make_cache_backend() -> CacheBackend:
    backend = os.environ.get("MDL_CACHE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return TTLCache()
    url = os.environ.get("MDL_CACHE_DB_URL", "").strip()
    if not url:
        raise RuntimeError(f"MDL_CACHE_BACKEND={backend} requires MDL_CACHE_DB_URL "
                           "(e.g. sqlite:///mdlaw_cache.db, mysql://u:p@host/db, "
                           "postgresql://u:p@host/db)")
    if backend in ("sqlite", "mysql", "postgres") and not url.startswith(
            ("sqlite:///", "mysql://", "postgresql://", "postgres://")):
        raise RuntimeError(f"MDL_CACHE_DB_URL scheme does not match backend {backend}")
    return SQLCache(url)


cache = _make_cache_backend()

# ---------------------------------------------------------------------------
# Upstream client
# ---------------------------------------------------------------------------
# Outbound throttle: WAF rate-limits bursts (validated: rapid probes -> soft 404).
_SEM = asyncio.Semaphore(2)
_last_out = 0.0
_out_lock = asyncio.Lock()
MIN_INTERVAL = 0.5  # seconds between upstream requests


async def _throttle() -> None:
    global _last_out
    async with _out_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_out)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_out = time.monotonic()


_client: httpx.AsyncClient | None = None
_cffi: Any | None = None


def _url(path: str) -> str:
    """Full upstream URL. httpx tolerates absolute URLs on a base_url client;
    curl_cffi's AsyncSession has no base_url support, so callers always pass
    the absolute URL."""
    return API_BASE + path


def get_client() -> httpx.AsyncClient | Any:
    """Return the shared upstream client. Default transport is curl_cffi
    (browser/mobile TLS impersonation — passes Cloudflare's JA3/JA4 challenge
    from flagged IPs). Set MDL_TRANSPORT=httpx to use a plain httpx TLS stack."""
    global _client, _cffi
    if TRANSPORT == "curl_cffi":
        if _cffi is None:
            try:
                from curl_cffi.requests import AsyncSession
            except ImportError:
                raise RuntimeError(
                    "MDL_TRANSPORT=curl_cffi (the default) requires the curl_cffi "
                    "package: pip install curl_cffi  (or set MDL_TRANSPORT=httpx)"
                ) from None
            _cffi = AsyncSession(impersonate="safari_ios", headers=default_headers())
        return _cffi
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=API_BASE,
            headers=default_headers(),
            timeout=20.0,
            limits=httpx.Limits(max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    """Close the shared upstream client (httpx or curl_cffi session)."""
    global _client, _cffi
    if _cffi is not None:
        try:
            await _cffi.close()
        except Exception:
            pass
        _cffi = None
    if _client is not None and not _client.is_closed:
        await _client.aclose()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup/shutdown: nothing to init (client is lazy); close the upstream
    client on shutdown so uvicorn/gunicorn can restart cleanly."""
    yield
    await close_client()


async def fetch(method: str, path: str, ttl: float,
                body: dict | None = None,
                cache_key: str | None = None,
                auth: bool = False) -> object:
    """GET/POST with cache + throttle. Returns parsed JSON (never caches errors).
    When auth=True and credentials are configured, sends a Bearer token and
    transparently refreshes + retries once on HTTP 401 (like the app's
    _renewTokenInterceptor)."""
    key = cache_key or f"{method} {path}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    if auth and not auth_enabled():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "this endpoint requires MDL_USERNAME and "
                                            "MDL_PASSWORD env vars"})
    headers = await auth_headers() if auth else None
    async with _SEM:
        await _throttle()
        try:
            r = await get_client().request(method, _url(path), headers=headers, json=body)
            if r.status_code == 401 and auth and auth_enabled():
                await refresh()
                headers = await auth_headers()
                r = await get_client().request(method, _url(path), headers=headers, json=body)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise _upstream_error(e.response.status_code, e.response.text) from e
        except httpx.TimeoutException as e:
            raise HTTPException(504, {"error": True, "code": 504,
                                      "detail": "upstream timeout"}) from e
        except httpx.HTTPError as e:
            raise HTTPException(502, {"error": True, "code": 502,
                                      "detail": f"upstream error: {e}"}) from e
    data = r.json()
    cache.put(key, data, ttl)
    return data

def _upstream_error(status: int, body: str) -> HTTPException:
    # Cloudflare bot-protection challenge (403 + "Just a moment..."). Tell the
    # user the fix instead of dumping raw HTML.
    if status == 403 and ("Just a moment" in body or "challenge" in body.lower()):
        return HTTPException(403, {"error": True, "code": 403,
                                   "detail": "Cloudflare blocked this request. Your IP is "
                                             "flagged — run with MDL_TRANSPORT=curl_cffi "
                                             "(pip install curl_cffi) to impersonate a "
                                             "browser/mobile TLS fingerprint."})
    try:
        import json
        detail = json.loads(body)
    except Exception:
        detail = body[:300]
    return HTTPException(status, {"error": True, "code": status, "detail": detail})


# ---------------------------------------------------------------------------
# Auth — single account via env vars, auto-login + auto-refresh
# ---------------------------------------------------------------------------
# The MDL app API requires a Bearer token for account features (title detail,
# watchlist, reviews, …). This wrapper logs in once with the account from the
# env and transparently refreshes the token when it expires (on HTTP 401 /
# invalid_grant), so every request can be authenticated. 2FA is NOT supported
# here — if your account has 2FA enabled, either disable it or use an app
# password. Reverse-engineered from REPORT.md §1 (auth flow).
MDL_USERNAME = os.environ.get("MDL_USERNAME", "").strip()
MDL_PASSWORD = os.environ.get("MDL_PASSWORD", "").strip()

_auth = {
    "token": None,
    "refresh_token": None,
    "device_id": str(uuid.uuid4()),
    "user": None,
    "expires_at": 0.0,
    "login_error": None,
    "last_login": None,
    "last_refresh": None,
    "refreshes": 0,
}
_auth_lock = asyncio.Lock()
# refresh guard: re-login instead of refresh when refresh fails
_REFRESHED = False


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def auth_enabled() -> bool:
    return bool(MDL_USERNAME and MDL_PASSWORD)


async def _auth_request(method: str, path: str, body: dict | None = None,
                        token: str | None = None) -> dict:
    """Direct upstream call for auth endpoints (bypasses cache; no auth header
    unless token passed). Returns parsed JSON or raises HTTPException."""
    h = default_headers(token=token)
    async with _SEM:
        await _throttle()
        try:
            r = await get_client().request(method, _url(path), headers=h, json=body)
        except httpx.HTTPStatusError as e:
            raise _upstream_error(e.response.status_code, e.response.text) from e
        except httpx.TimeoutException as e:
            raise HTTPException(504, {"error": True, "code": 504,
                                      "detail": "upstream timeout"}) from e
        except httpx.HTTPError as e:
            raise HTTPException(502, {"error": True, "code": 502,
                                      "detail": f"upstream error: {e}"}) from e
    return r.json()


async def login(force: bool = False) -> dict:
    """Login with env credentials. Cached until forced or token expires."""
    async with _auth_lock:
        if (not force and _auth["token"]
                and _auth["expires_at"] > time.time()):
            return _auth
        if not auth_enabled():
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": "MDL_USERNAME/MDL_PASSWORD not set"})
        try:
            data = await _auth_request(
                "POST", f"/auth/login?device_id={_auth['device_id']}",
                body={"username": MDL_USERNAME, "password": _md5(MDL_PASSWORD)})
            if "challenge_id" in data or "2fa" in str(data).lower():
                raise HTTPException(428, {"error": True, "code": 428,
                                          "detail": "2FA required on this account — "
                                                    "disable 2FA or use an app password"})
            if "access_token" not in data and "token" not in data:
                raise HTTPException(400, {"error": True, "code": 400,
                                          "detail": f"login response missing token: {data}"})
        except HTTPException as e:
            _auth["login_error"] = str(e.detail)
            _auth["last_login"] = None
            raise
        _set_tokens(data)
        _auth["login_error"] = None
        _auth["last_login"] = time.time()
        _save_auth()
        return _auth


def _set_tokens(data: dict) -> None:
    _auth["token"] = data.get("access_token") or data.get("token")
    _auth["refresh_token"] = data.get("refresh_token")
    _auth["user"] = data.get("user")
    # Prefer server-provided lifetime; else decode JWT exp; else 6h fallback.
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        _auth["expires_at"] = time.time() + float(expires_in)
        return
    try:
        import base64
        payload = _auth["token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        _auth["expires_at"] = float(exp)
    except Exception:
        _auth["expires_at"] = time.time() + 6 * 3600


async def refresh() -> dict:
    """Refresh token; if refresh fails, re-login. Returns _auth dict."""
    global _REFRESHED
    async with _auth_lock:
        if not _auth["refresh_token"]:
            return await login(force=True)
        try:
            data = await _auth_request(
                "POST", f"/auth/refresh?device_id={_auth['device_id']}",
                body={"refresh_token": _auth["refresh_token"]},
                token=_auth["token"])
            _set_tokens(data)
            _auth["last_refresh"] = time.time()
            _auth["refreshes"] += 1
            _REFRESHED = True
            _save_auth()
        except HTTPException:
            _REFRESHED = False
            return await login(force=True)
        return _auth


async def auth_headers() -> dict[str, str]:
    """Headers with a valid Bearer token; auto-login/refresh when needed."""
    if not auth_enabled():
        return default_headers()
    if not _auth["token"] or _auth["expires_at"] <= time.time() + 60:
        if _auth["refresh_token"]:
            await refresh()
        else:
            await login()
    return default_headers(token=_auth["token"])


# Saved CLI session — `mdlaw auth` stores the token so later commands reuse it.
_AUTH_FILE = os.path.join(os.path.expanduser("~"), ".mdlaw_auth.json")


def _save_auth() -> None:
    """Persist the current session to ~/.mdlaw_auth.json (chmod 600)."""
    data = {
        "token": _auth["token"],
        "refresh_token": _auth["refresh_token"],
        "device_id": _auth["device_id"],
        "expires_at": _auth["expires_at"],
        "user": _auth["user"],
    }
    with open(_AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(_AUTH_FILE, 0o600)
    except OSError:
        pass


def _load_auth() -> bool:
    """Load a saved CLI session; returns True if a session was restored.
    Marks credentials as configured (dummy values) so auth_enabled() is True
    while the saved token/refresh_token drive the actual requests."""
    global MDL_USERNAME, MDL_PASSWORD
    try:
        with open(_AUTH_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    _auth.update({k: data[k] for k in
                  ("token", "refresh_token", "device_id", "expires_at", "user")
                  if k in data})
    if not MDL_USERNAME:
        MDL_USERNAME = "saved"
    if not MDL_PASSWORD:
        MDL_PASSWORD = "saved"
    return True
app = FastAPI(
    title="mdlaw — MyDramaList API Wrapper",
    version="1.0.0",
    description=(
        "Blazing-fast unofficial API for MyDramaList, powered by the official "
        "MDL Android app API.\n\n"
        "**Interactive docs:**\n"
        "- [Swagger UI (playground)](/docs) — try every endpoint live\n"
        "- [ReDoc](/redoc) — clean reference docs\n"
        "- [OpenAPI JSON](/openapi.json) — machine-readable spec\n\n"
        "Not affiliated with MyDramaList."
    ),
    lifespan=lifespan,
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,   # hide schemas section, focus on endpoints
        "tryItOutEnabled": True,          # playground open by default
        "displayRequestDuration": True,   # show latency per request
    },
)

_START = time.time()


@app.get("/", include_in_schema=False)
async def root():
    """Landing: point humans at the interactive docs."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


def _resp(data: object, ttl: float) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={int(ttl)}"})


@app.get("/api/v1/auth/status")
async def auth_status() -> dict:
    """Auth state: configured? logged in? token expiry, last refresh."""
    return {
        "configured": auth_enabled(),
        "logged_in": bool(_auth["token"]),
        "user": _auth["user"],
        "expires_at": _auth["expires_at"],
        "last_login": _auth["last_login"],
        "last_refresh": _auth["last_refresh"],
        "refreshes": _auth["refreshes"],
        "login_error": _auth["login_error"],
    }


@app.post("/api/v1/auth/login", include_in_schema=False)
async def auth_login() -> dict:
    """Force a fresh login (credentials from env). Returns token state."""
    return await login(force=True)


@app.post("/api/v1/auth/refresh", include_in_schema=False)
async def auth_refresh() -> dict:
    """Force a token refresh (or re-login if refresh fails)."""
    return await refresh()


@app.get("/api/v1/cache/stats")
async def cache_stats() -> dict:
    """Cache backend info + hit stats + change-detection count."""
    return {
        "backend": getattr(cache, "_flavor", "memory"),
        "hits": cache.hits,
        "misses": cache.misses,
        "hit_rate": round(cache.hits / (cache.hits + cache.misses), 4) if (cache.hits + cache.misses) else None,
        "entries": cache.entries(),
    }


@app.get("/api/v1/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "mdlaw",
        "auth": auth_enabled(),
    }


@app.get("/api/v1/dashboard")
async def dashboard() -> dict:
    """Health dashboard: uptime, cache stats, per-route registry."""
    total = cache.hits + cache.misses
    return {
        "status": "ok",
        "service": "mdlaw",
        "version": __version__,
        "uptime_seconds": int(time.time() - _START),
        "cache": {
            "hits": cache.hits,
            "misses": cache.misses,
            "hit_rate": round(cache.hits / total, 4) if total else None,
            "entries": cache.entries(),
            "backend": getattr(cache, "_flavor", "memory"),
        },
        "upstream": {
            "base": API_BASE,
            "key_source": "env" if os.environ.get("MDL_API_KEY") else "generated-nonce",
            "transport": TRANSPORT,
        },
        "auth": {
            "configured": auth_enabled(),
            "logged_in": bool(_auth["token"]),
            "refreshes": _auth["refreshes"],
        },
        "routes": [
            {
                "path": r.path,
                "methods": sorted(r.methods or []),
            }
            for r in app.routes
            if getattr(r, "path", "").startswith("/api/v1")
        ],
    }


@app.get("/api/v1/status")
async def status() -> JSONResponse:
    """Alias of /dashboard kept for monitoring probes."""
    return JSONResponse(await dashboard())


@app.get("/api/v1/genres")
async def genres() -> JSONResponse:
    return _resp(await fetch("GET", "/genres", ttl=3600), 3600)


@app.get("/api/v1/languages")
async def languages() -> JSONResponse:
    return _resp(await fetch("GET", "/languages/supported?v=2", ttl=3600), 3600)


@app.get("/api/v1/titles/{tid}/reviews")
async def title_reviews(tid: int) -> JSONResponse:
    return _resp(await fetch("GET", f"/titles/{tid}/reviews", ttl=300), 300)


@app.get("/api/v1/titles/{tid}")
async def title_detail(tid: int) -> JSONResponse:
    """Title detail — requires auth (upstream 400s without a token)."""
    return _resp(await fetch("GET", f"/titles/{tid}?expand=1", ttl=300, auth=True), 300)


@app.get("/api/v1/titles/{tid}/recommendations")
async def title_recommendations(tid: int) -> JSONResponse:
    return _resp(await fetch("GET", f"/titles/{tid}/recommendations", ttl=600, auth=True), 600)


@app.post("/api/v1/search")
async def search(q: str) -> JSONResponse:
    """Search titles by keyword. MDL moved to POST + JSON body (GET now 405)."""
    if not q.strip():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "query param q is required"})
    return _resp(await fetch("POST", "/search", ttl=300, auth=True,
                             body={"q": q, "synopsis": 1}), 300)


@app.post("/api/v1/search/people")
async def search_people(q: str) -> JSONResponse:
    """Search people by name. MDL moved to POST + JSON body (GET now 405)."""
    if not q.strip():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "query param q is required"})
    return _resp(await fetch("POST", "/search/people", ttl=300, auth=True,
                             body={"q": q}), 300)


@app.get("/api/v1/watchlist")
async def watchlist() -> JSONResponse:
    """Current user's watchlist (requires auth)."""
    return _resp(await fetch("GET", "/sync/mylist/watchlist", ttl=60, auth=True), 60)


@app.get("/api/v1/watchlist/{status}")
async def watchlist_by_status(status: str) -> JSONResponse:
    """Watchlist filtered by status: completed|dropped|onhold|plantowatch|notinterested|undecided."""
    valid = ("completed", "dropped", "onhold", "plantowatch", "notinterested", "undecided")
    if status not in valid:
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": f"status must be one of {valid}"})
    return _resp(await fetch("GET", f"/sync/mylist/{status}", ttl=60, auth=True), 60)


@app.get("/api/v1/me")
async def me() -> JSONResponse:
    """Current account profile (requires auth)."""
    return _resp(await fetch("GET", "/users/me", ttl=60, auth=True), 60)


@app.get("/api/v1/titles/{tid}/comments")
async def title_comments(tid: int) -> JSONResponse:
    return _resp(await fetch("GET", f"/titles/{tid}/comments", ttl=300), 300)


@app.get("/api/v1/titles/{tid}/credits")
async def title_credits(tid: int) -> JSONResponse:
    # Auth-gated upstream (400 without token); same clear error as other
    # auth-gated endpoints.
    return _resp(await fetch("GET", f"/titles/{tid}/credits", ttl=300, auth=True), 300)


@app.get("/api/v1/calendar")
async def calendar() -> JSONResponse:
    return _resp(await fetch("POST", "/calendar/episodes", body={}, ttl=3600), 3600)


@app.get("/api/v1/articles/featured")
async def articles_featured(page: int = 1) -> JSONResponse:
    return _resp(await fetch("GET", f"/articles/featured?page={page}", ttl=600), 600)


@app.get("/api/v1/lists/featured")
async def lists_featured(limit: int = 5) -> JSONResponse:
    return _resp(await fetch("GET", f"/lists/featured?limit={limit}", ttl=600), 600)


@app.get("/api/v1/lists/popular")
async def lists_popular(limit: int = 5) -> JSONResponse:
    return _resp(await fetch("GET", f"/lists/popular_voting_lists?limit={limit}", ttl=600), 600)


@app.get("/api/v1/people/leaderboard")
async def leaderboard(period: str = "alltime") -> JSONResponse:
    if period not in ("alltime", "weekly", "monthly"):
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "period must be alltime|weekly|monthly"})
    return _resp(await fetch("GET", f"/people/leaderboard?time_period={period}", ttl=600), 600)


@app.get("/api/v1/people/{pid}")
async def people(pid: int) -> JSONResponse:
    return _resp(await fetch("GET", f"/people/{pid}", ttl=86400), 86400)


@app.get("/api/v1/payment/plans")
async def payment_plans() -> JSONResponse:
    return _resp(await fetch("GET", "/payment/plans", ttl=3600), 3600)


@app.get("/api/v1/payment/coins")
async def payment_coins() -> JSONResponse:
    return _resp(await fetch("GET", "/payment/coins", ttl=3600), 3600)


# ---------------------------------------------------------------------------
# Python package layer — use mdlaw as a library without the HTTP server.
#   from mdlaw import MDL
#   mdl = MDL()
#   genres = await mdl.genres()
#   title = await mdl.title(686)
# Each method wraps fetch() → shared cache, throttle, auth, transport.
# ---------------------------------------------------------------------------
class MDL:
    """Pythonic client for the MDL app API (no server needed).

    All methods are async and share the module-level cache, throttle, auth
    and transport. Construct once, call many, `await mdl.close()` at exit.
    """

    def __init__(self, transport: str | None = None,
                 username: str | None = None, password: str | None = None):
        if transport:
            global TRANSPORT
            TRANSPORT = transport
        if username is not None or password is not None:
            global MDL_USERNAME, MDL_PASSWORD
            MDL_USERNAME = (username or "").strip()
            MDL_PASSWORD = (password or "").strip()
        # No env credentials → reuse a session saved by `mdlaw auth`.
        if not auth_enabled() and not _auth["token"]:
            _load_auth()

    async def get(self, path: str, ttl: float = 3600, auth: bool = False) -> object:
        return await fetch("GET", path, ttl=ttl, auth=auth)

    async def post(self, path: str, body: dict, ttl: float = 300,
                   auth: bool = False) -> object:
        return await fetch("POST", path, ttl=ttl, auth=auth, body=body)

    # --- public data ---
    async def genres(self) -> object:
        return await self.get("/genres", ttl=3600)

    async def languages(self) -> object:
        return await self.get("/languages/supported?v=2", ttl=3600)

    async def calendar(self) -> object:
        return await self.post("/calendar/episodes", {}, ttl=3600)

    async def articles_featured(self, page: int = 1) -> object:
        return await self.get(f"/articles/featured?page={page}", ttl=600)

    async def lists_featured(self, limit: int = 5) -> object:
        return await self.get(f"/lists/featured?limit={limit}", ttl=600)

    async def lists_popular(self, limit: int = 5) -> object:
        return await self.get(f"/lists/popular_voting_lists?limit={limit}", ttl=600)

    async def leaderboard(self, period: str = "alltime") -> object:
        return await self.get(f"/people/leaderboard?time_period={period}", ttl=600)

    async def people(self, pid: int) -> object:
        return await self.get(f"/people/{pid}", ttl=86400)

    async def payment_plans(self) -> object:
        return await self.get("/payment/plans", ttl=3600)

    async def payment_coins(self) -> object:
        return await self.get("/payment/coins", ttl=3600)

    # --- titles ---
    async def title(self, tid: int) -> object:
        """Title detail — requires account credentials (auth-gated upstream)."""
        return await self.get(f"/titles/{tid}?expand=1", ttl=300, auth=True)

    async def title_reviews(self, tid: int) -> object:
        return await self.get(f"/titles/{tid}/reviews", ttl=300)

    async def title_recommendations(self, tid: int) -> object:
        return await self.get(f"/titles/{tid}/recommendations", ttl=600, auth=True)

    async def title_comments(self, tid: int) -> object:
        return await self.get(f"/titles/{tid}/comments", ttl=300)

    async def title_credits(self, tid: int) -> object:
        return await self.get(f"/titles/{tid}/credits", ttl=300, auth=True)

    # --- search (POST) ---
    # NOTE: verified live (2026-09) — the upstream POST /search ignores `q`
    # entirely and always returns the same default 20-item feed (no server-side
    # filtering, no pagination). We keep the parameter for API compat and add
    # client-side post-filtering on the fields search results DO carry
    # (country, language, type, media_type, year).
    async def search(self, q: str = "", country: str | None = None,
                     language: str | None = None, type: str | None = None,
                     media_type: str | None = None, year: int | None = None,
                     limit: int | None = None) -> object:
        """Search titles and optionally post-filter results client-side.

        The upstream API has no server-side filters (verified live: q, genre_id,
        language_id, page, limit, sort are all ignored — it always returns the
        same 20 default items). Filters below are applied in Python to the fields
        search results carry: ``country``, ``language``, ``type``, ``media_type``,
        ``year``. Genre is NOT filterable here (search items have no genres field)
        — use :meth:`browse_by_genre` instead.
        """
        results = await self.post("/search", {"q": q, "synopsis": 1},
                                  ttl=300, auth=True)
        if not isinstance(results, list):
            return results
        filtered = results
        if country:
            filtered = [it for it in filtered
                        if (it.get("country") or "").lower() == country.lower()]
        if language:
            filtered = [it for it in filtered
                        if (it.get("language") or "").lower() == language.lower()]
        if type:
            filtered = [it for it in filtered
                        if (it.get("type") or "").lower() == type.lower()]
        if media_type:
            filtered = [it for it in filtered
                        if (it.get("media_type") or "").lower() == media_type.lower()]
        if year:
            filtered = [it for it in filtered if it.get("year") == int(year)]
        return filtered[:limit] if limit else filtered

    async def browse_by_genre(self, genre_id: int, limit: int = 10,
                              source: str = "search") -> list:
        """Fetch titles and keep only those whose detail includes ``genre_id``.

        The upstream API has no server-side genre filter, and search results
        carry no genres field — the only place ``genres[]`` appears is the
        title-detail endpoint. So this fetches each candidate's detail
        (auth-gated) and filters client-side. ``source`` picks the candidate
        pool: ``"search"`` (default feed), ``"trending"`` or ``"top_movies"``.
        Each detail fetch is a separate upstream call — keep ``limit`` small.
        """
        pool: object
        if source == "search":
            pool = await self.search("")          # default feed (q ignored upstream)
        elif source == "trending":
            pool = await self.get(f"/titles/trending?limit={max(limit * 2, 10)}",
                                  ttl=300, auth=True)
        elif source == "top_movies":
            pool = await self.get(f"/titles/top_movies?limit={max(limit * 2, 10)}",
                                  ttl=300, auth=True)
        else:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": "source must be 'search', 'trending' or 'top_movies'"})
        candidates = pool if isinstance(pool, list) else (pool.get("items", []) if isinstance(pool, dict) else [])
        out: list = []
        for item in candidates:
            tid = item.get("id") or item.get("rid")
            if not tid:
                continue
            try:
                detail = await self.title(tid)
            except Exception:
                continue
            genres = detail.get("genres") or [] if isinstance(detail, dict) else []
            gids = [g.get("id") for g in genres]
            if genre_id in gids:
                out.append(detail)
                if len(out) >= limit:
                    break
        return out

    async def search_people(self, q: str) -> object:
        return await self.post("/search/people", {"q": q}, ttl=300, auth=True)

    # --- account (requires credentials) ---
    _WATCH_STATUSES = ("completed", "dropped", "onhold", "plantowatch",
                       "notinterested", "undecided")

    async def watchlist(self, status: str | None = None) -> object:
        if status and status not in self._WATCH_STATUSES:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": f"status must be one of {self._WATCH_STATUSES}"})
        path = f"/sync/mylist/{status}" if status else "/sync/mylist/watchlist"
        return await self.get(path, ttl=60, auth=True)

    async def me(self) -> object:
        return await self.get("/users/me", ttl=60, auth=True)

    # --- lifecycle ---
    async def close(self) -> None:
        await close_client()

    def stats(self) -> dict:
        return cache.stats()


# ---------------------------------------------------------------------------
# Offline self-check
# ---------------------------------------------------------------------------
def self_check() -> int:
    # API_KEY is always set: from MDL_API_KEY env, or a generated nonce.
    assert API_KEY and len(API_KEY) == 20, "API key must be a 20-char nonce"
    assert TRANSPORT in ("httpx", "curl_cffi"), f"unknown MDL_TRANSPORT={TRANSPORT}"
    h = default_headers()
    assert h["mdl-api-key"] == API_KEY
    assert h["User-Agent"] == "okhttp/4.12.0" and h["Accept"] == "*/*"
    assert "Authorization" not in h
    assert default_headers("t")["Authorization"] == "Bearer t"
    assert _md5("secret") == "5ebe2294ecd0e0f08eab7690d2a6ee69", "md5"
    c = TTLCache()
    c.put("k", {"a": 1}, 100)
    assert c.get("k") == {"a": 1}
    c.put("e", 1, -1)
    assert c.get("e") is None
    assert cache.get("never") is None
    # SQL cache backend (sqlite in-memory): put/get + change detection
    s = SQLCache("sqlite:///:memory:")
    s.put("k", {"a": 1}, 100)
    assert s.get("k") == {"a": 1}
    s.put("k", {"a": 2}, 100)          # changed value
    cur = s._cursor()
    cur.execute("SELECT changed FROM mdlaw_cache WHERE key = 'k'")
    row = cur.fetchone()
    assert row is not None and row[0] == 1, "change detection must flag changed value"
    cur.close()
    s.put("k", {"a": 2}, 100)          # same value
    cur = s._cursor()
    cur.execute("SELECT changed FROM mdlaw_cache WHERE key = 'k'")
    row = cur.fetchone()
    assert row is not None and row[0] == 0, "same value must not flag changed"
    cur.close()
    print(f"PASS: offline checks (key={'env' if os.environ.get('MDL_API_KEY') else 'nonce'} len={len(API_KEY)}, "
          f"transport={TRANSPORT}, headers, cache, md5, sqlcache)")
    return 0


_CLI_USAGE = """\
mdlaw — MyDramaList API wrapper CLI

Usage:
  mdlaw [command] [args...]

Commands:
  auth                      Log in and save the session to ~/.mdlaw_auth.json
                            (prompts for username/password). Later commands
                            reuse the saved token and auto-refresh it.
  auth status               Show saved session info (user, token expiry).
  logout                    Remove the saved session.
  genres                    List all genres.
  languages                 List supported languages.
  calendar                  Upcoming episodes.
  search <query>            Search titles (requires auth).
  search-people <name>      Search people (requires auth).
  title <id>                Title detail (requires auth).
  people <id>               Actor/crew profile.
  watchlist [status]        Your watchlist (requires auth).
  me                        Your profile (requires auth).
  leaderboard [period]      Period: alltime | weekly | monthly.
  self                      Offline self-check.
  serve | run               Start the HTTP server (default when no command).

Options:
  -h, --help                Show this help.
  --transport <httpx|curl_cffi>   Transport override (CLI default: curl_cffi).

Environment:
  MDL_USERNAME / MDL_PASSWORD     Account credentials (alternative to `auth`).
  MDL_TRANSPORT                   Transport for the server / library
                                  (default: httpx there; CLI defaults to curl_cffi).
"""


def _run_cli(args: list[str]) -> int:
    """One-shot data commands: mdlaw genres, mdlaw search 'q', mdlaw title 686 ..."""
    import asyncio
    import json as _json
    import sys

    # CLI defaults to curl_cffi (passes Cloudflare from flagged IPs) unless the
    # user explicitly overrides with --transport or MDL_TRANSPORT.
    global TRANSPORT
    if not os.environ.get("MDL_TRANSPORT"):
        TRANSPORT = "curl_cffi"
    if args and args[0] == "--transport":
        if len(args) < 2:
            print("error: --transport requires httpx or curl_cffi", file=sys.stderr)
            return 1
        TRANSPORT = args[1].lower()
        args = args[2:]
    if args and args[0] in ("-h", "--help"):
        print(_CLI_USAGE)
        return 0
    if not args:
        print(_CLI_USAGE)
        return 0

    cmd, rest = args[0], args[1:]

    if cmd == "auth":
        return _cli_auth(rest)
    if cmd == "logout":
        return _cli_logout()

    # Load the saved session for auth-gated commands.
    if not auth_enabled():
        _load_auth()

    async def call():
        mdl = MDL()
        try:
            if cmd == "genres":
                return await mdl.genres()
            if cmd == "languages":
                return await mdl.languages()
            if cmd == "calendar":
                return await mdl.calendar()
            if cmd == "search":
                if not rest:
                    raise SystemExit("usage: mdlaw search <query>")
                return await mdl.search(" ".join(rest))
            if cmd == "search-people":
                if not rest:
                    raise SystemExit("usage: mdlaw search-people <name>")
                return await mdl.search_people(" ".join(rest))
            if cmd == "title":
                if not rest:
                    raise SystemExit("usage: mdlaw title <id>")
                return await mdl.title(int(rest[0]))
            if cmd == "people":
                if not rest:
                    raise SystemExit("usage: mdlaw people <id>")
                return await mdl.people(int(rest[0]))
            if cmd == "watchlist":
                return await mdl.watchlist(rest[0] if rest else None)
            if cmd == "me":
                return await mdl.me()
            if cmd == "leaderboard":
                return await mdl.leaderboard(rest[0] if rest else "alltime")
            raise SystemExit(f"unknown command: {cmd}")
        finally:
            await mdl.close()

    try:
        data = asyncio.run(call())
    except HTTPException as e:
        print(f"error: {e.detail}", file=sys.stderr)
        return 1
    print(_json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _cli_auth(rest: list[str]) -> int:
    """mdlaw auth [username] — log in and save the session."""
    import getpass
    import sys

    if rest and rest[0] == "status":
        return _cli_auth_status()

    username = rest[0] if rest else None
    if not username:
        username = input("MDL username/email: ").strip()
    if not username:
        print("error: username required", file=sys.stderr)
        return 1
    password = getpass.getpass("MDL password: ")

    global MDL_USERNAME, MDL_PASSWORD
    MDL_USERNAME = username
    MDL_PASSWORD = password

    async def do_login():
        try:
            await login(force=True)
            return True
        except HTTPException as e:
            print(f"error: {e.detail}", file=sys.stderr)
            return False
        finally:
            await close_client()

    if not asyncio.run(do_login()):
        return 1
    print(f"logged in as {username}; session saved to {_AUTH_FILE}")
    return 0


def _cli_auth_status() -> int:
    """Show saved session info."""
    if not _load_auth() or not _auth["token"]:
        print("not logged in (run `mdlaw auth`)")
        return 0
    user = _auth["user"] if isinstance(_auth["user"], dict) else {}
    name = user.get("name") or user.get("username") or ""
    remaining = max(0, int(_auth["expires_at"] - time.time()))
    print(f"logged in as: {name}")
    print(f"token expires in: {remaining // 3600}h {remaining % 3600 // 60}m")
    print(f"device_id: {_auth['device_id']}")
    print(f"session file: {_AUTH_FILE}")
    return 0


def _cli_logout() -> int:
    """Remove the saved session."""
    try:
        os.remove(_AUTH_FILE)
        print("logged out; session file removed")
    except OSError:
        print("no saved session")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (console script `mdlaw`)."""
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if args and args[0] == "self":
        return self_check()
    if args and args[0] in ("serve", "run"):
        args = args[1:]
    if args and args[0] not in ("serve", "run"):
        return _run_cli(args)
    import uvicorn
    uvicorn.run("mdlaw:app", host="0.0.0.0", port=8000)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
