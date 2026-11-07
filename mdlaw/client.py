"""Upstream client and auth (mutually dependent: fetch() needs auth headers
and refresh; login/refresh go through _send()). Also the browse filter tables
and browse_path(), shared by the server, the library and the CLI.

Globals reassigned at runtime (TRANSPORT, MDL_USERNAME, MDL_PASSWORD,
_PERSIST_AUTH, _last_out) live here: other modules read and write them as
``client.NAME``, never via ``from .client import NAME``."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
import weakref
from typing import Any

import httpx
from fastapi import HTTPException

from . import caching
from ._config import API_BASE, _env_int, _qs, default_headers, log

# Transport. The real gate on the MDL API is Cloudflare bot protection, which
# fingerprints the TLS/HTTP2 handshake (JA3/JA4). Default is curl_cffi
# (browser/mobile TLS impersonation), which passes the challenge from
# flagged/datacenter IPs. Set MDL_TRANSPORT=httpx for a plain httpx TLS stack
# (lighter, no curl_cffi needed) — works from most residential IPs.
TRANSPORT = os.environ.get("MDL_TRANSPORT", "curl_cffi").strip().lower()


# ---------------------------------------------------------------------------
# Upstream client
# ---------------------------------------------------------------------------
# Outbound throttle: WAF rate-limits bursts (validated: rapid probes -> soft 404).
MAX_CONCURRENCY = 2  # parallel upstream requests
MIN_INTERVAL = 0.5   # seconds between upstream requests
_last_out = 0.0


class _LoopState:
    """Everything bound to an event loop: asyncio locks, the HTTP client and
    in-flight tasks. Library code may run several loops over its lifetime
    (each asyncio.run() makes a new one) — reusing a client or lock from a
    finished loop fails with "Event loop is closed", so each loop gets its
    own set. Auth tokens and the cache stay process-wide."""

    def __init__(self) -> None:
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.out_lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.httpx: httpx.AsyncClient | None = None
        self.cffi: Any | None = None
        self.inflight: dict[str, asyncio.Task] = {}


_loop_states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _state() -> _LoopState:
    """State for the running event loop (created on first use)."""
    loop = asyncio.get_running_loop()
    st = _loop_states.get(loop)
    if st is None:
        st = _loop_states[loop] = _LoopState()
    return st


async def _throttle() -> None:
    global _last_out
    async with _state().out_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_out)
        if wait > 0:
            log.debug("throttle: waiting %.2fs before the next request", wait)
            await asyncio.sleep(wait)
        _last_out = time.monotonic()


def _url(path: str) -> str:
    """Full upstream URL. httpx tolerates absolute URLs on a base_url client;
    curl_cffi's AsyncSession has no base_url support, so callers always pass
    the absolute URL."""
    return API_BASE + path


def get_client() -> httpx.AsyncClient | Any:
    """Return the running loop's upstream client. Default transport is
    curl_cffi (browser/mobile TLS impersonation — passes Cloudflare's JA3/JA4
    challenge from flagged IPs). Set MDL_TRANSPORT=httpx to use a plain httpx
    TLS stack. Must be called from inside a running event loop."""
    st = _state()
    if TRANSPORT == "curl_cffi":
        if st.cffi is None:
            try:
                from curl_cffi.requests import AsyncSession
            except ImportError:
                raise RuntimeError(
                    "MDL_TRANSPORT=curl_cffi (the default) requires the curl_cffi "
                    "package: pip install curl_cffi  (or set MDL_TRANSPORT=httpx)"
                ) from None
            st.cffi = AsyncSession(impersonate="safari_ios", headers=default_headers())
        return st.cffi
    if st.httpx is None or st.httpx.is_closed:
        st.httpx = httpx.AsyncClient(
            base_url=API_BASE,
            headers=default_headers(),
            timeout=20.0,
            limits=httpx.Limits(max_keepalive_connections=10),
        )
    return st.httpx


async def close_client() -> None:
    """Close the running loop's upstream client(s) (httpx and/or curl_cffi)."""
    st = _loop_states.get(asyncio.get_running_loop())
    if st is None:
        return
    if st.cffi is not None:
        try:
            await st.cffi.close()
        except Exception:  # best-effort teardown; nothing useful to do on failure
            pass
        st.cffi = None
    if st.httpx is not None and not st.httpx.is_closed:
        await st.httpx.aclose()
    st.httpx = None


def _transport_errors() -> tuple[tuple[type, ...], tuple[type, ...]]:
    """(timeout types, other transport-error types) for every transport.

    curl_cffi (the default) has its own exception hierarchy, unrelated to
    httpx's — catching only httpx.* let curl_cffi failures escape as bare 500s.
    """
    timeouts: list[type] = [httpx.TimeoutException]
    errors: list[type] = [httpx.HTTPError]
    try:
        from curl_cffi.requests import exceptions as cffi_exc
    except ImportError:
        pass
    else:
        timeouts.append(cffi_exc.Timeout)
        errors.append(cffi_exc.RequestException)
    return tuple(timeouts), tuple(errors)


_TIMEOUT_ERRORS, _TRANSPORT_ERRORS = _transport_errors()

# Bounded retry for transient upstream failures (timeouts, connection errors,
# 429, 502/503/504) on reads made through fetch(). MDL_RETRIES=0 disables it.
# Auth calls (login/refresh) go through _auth_request() and are never retried.
RETRIES = max(0, _env_int("MDL_RETRIES", 2))
RETRY_BASE_DELAY = 0.5     # seconds; doubled per attempt, with jitter
RETRY_MAX_DELAY = 10.0     # cap for both backoff and an upstream Retry-After
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
# POSTs that only read: the app's search/browse and calendar endpoints take
# their params as a query string and change nothing server-side (their
# responses are cached by fetch() like GETs). Any other POST is not retried.
_READ_ONLY_POSTS = ("/search/", "/calendar/episodes")


def _retryable(method: str, path: str) -> bool:
    """Only idempotent reads are retried: GET, plus the read-only POSTs."""
    method = method.upper()
    if method == "GET":
        return True
    return method == "POST" and path.startswith(_READ_ONLY_POSTS)


def _retry_after(r: Any) -> float | None:
    """Seconds from an upstream Retry-After header (delta or HTTP-date)."""
    try:
        raw = r.headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter for retry number ``attempt`` (1-based)."""
    delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * 2 ** (attempt - 1))
    return secrets.SystemRandom().uniform(delay / 2, delay)


async def _retry_sleep(seconds: float) -> None:
    """Indirection so tests can skip the real wait."""
    await asyncio.sleep(seconds)


async def _send(method: str, path: str, headers: dict | None = None,
                body: dict | None = None) -> Any:
    """One throttled upstream request. Transport failures become 502/504;
    the HTTP status is left for _parse() to judge."""
    async with _state().sem:
        await _throttle()
        t0 = time.monotonic()
        try:
            r = await get_client().request(method, _url(path), headers=headers, json=body)
        except _TIMEOUT_ERRORS as e:
            log.warning("%s %s timed out after %.1fs", method, path, time.monotonic() - t0)
            raise HTTPException(504, {"error": True, "code": 504,
                                      "detail": "upstream timeout"}) from e
        except _TRANSPORT_ERRORS as e:
            log.warning("%s %s failed: %s", method, path, e)
            raise HTTPException(502, {"error": True, "code": 502,
                                      "detail": f"upstream error: {e}"}) from e
    ms = (time.monotonic() - t0) * 1000
    level = logging.WARNING if r.status_code >= 500 else logging.INFO
    log.log(level, "%s %s -> %d (%.0f ms, %s)", method, path, r.status_code, ms,
            _size(len(r.content or b"")))
    return r


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _parse(r: Any) -> object:
    """Status check + JSON decode for an httpx or curl_cffi response."""
    if r.status_code >= 400:
        try:
            text = r.text
        except Exception:
            text = r.content.decode("utf-8", errors="replace")
        raise _upstream_error(r.status_code, text)
    try:
        return r.json()
    except (UnicodeDecodeError, ValueError):
        pass
    # Some profiles contain non-UTF-8 bytes (e.g. Arabic display names served
    # as ISO-8859-1/Windows-1252). Decode leniently instead of 500.
    try:
        return json.loads(r.content.decode("utf-8", errors="replace"))
    except ValueError:
        raise HTTPException(502, {"error": True, "code": 502,
                                  "detail": "upstream returned a non-JSON response"}) from None


async def fetch(method: str, path: str, ttl: float,
                body: dict | None = None,
                cache_key: str | None = None,
                auth: bool = False) -> object:
    """GET/POST with cache + throttle. Returns parsed JSON (never caches errors).
    When auth=True and credentials are configured, sends a Bearer token and
    transparently refreshes + retries once on HTTP 401 (like the app's
    _renewTokenInterceptor). Reads (GET and read-only search POSTs) are also
    retried up to RETRIES times on transient failures (MDL_RETRIES)."""
    key = cache_key or f"{method} {path}"
    hit = await caching._cache_call(caching.cache.get, key)
    if hit is not None:
        log.debug("cache hit: %s", key)
        return hit
    log.debug("cache miss: %s", key)
    if auth and not auth_enabled():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "this endpoint requires MDL_USERNAME and "
                                            "MDL_PASSWORD env vars"})
    # Concurrent misses for the same key share one upstream request instead of
    # each queueing behind the throttle.
    inflight = _state().inflight
    task = inflight.get(key)
    if task is not None:
        log.debug("joining in-flight request: %s", key)
    else:
        task = asyncio.ensure_future(_fetch_upstream(method, path, ttl, body, key, auth))
        inflight[key] = task

        def _done(t: asyncio.Task, k: str = key) -> None:
            if inflight.get(k) is t:
                del inflight[k]
        task.add_done_callback(_done)
    # shield: one caller disconnecting must not cancel the shared request
    return await asyncio.shield(task)


async def _fetch_upstream(method: str, path: str, ttl: float, body: dict | None,
                          key: str, auth: bool) -> object:
    headers = await auth_headers() if auth else None
    sent_token = _auth["token"]
    # One budget for the whole call: at most 1 + retries + 1 (the 401
    # refresh) upstream requests, never retries x refreshes.
    retries = RETRIES if _retryable(method, path) else 0
    attempt = 0
    refreshed = False
    while True:
        try:
            r = await _send(method, path, headers, body)
        except HTTPException as e:
            # _send only raises for transport failures (502 error / 504 timeout)
            if attempt >= retries:
                raise
            attempt += 1
            delay = _backoff_delay(attempt)
            log.warning("%s %s: transport error (HTTP %d); retry %d/%d in %.1fs",
                        method, path, e.status_code, attempt, retries, delay)
            await _retry_sleep(delay)
            continue
        if r.status_code == 401 and auth and auth_enabled() and not refreshed:
            log.info("upstream rejected the token (401); refreshing and retrying once")
            refreshed = True
            await refresh(stale_token=sent_token)
            headers = await auth_headers()
            continue
        if r.status_code in _RETRY_STATUSES and attempt < retries:
            wait = _retry_after(r) if r.status_code in (429, 503) else None
            if wait is not None and wait > RETRY_MAX_DELAY:
                # Retrying sooner than upstream asked only feeds the WAF.
                log.warning("%s %s -> %d; Retry-After %.0fs exceeds %.0fs, not retrying",
                            method, path, r.status_code, wait, RETRY_MAX_DELAY)
                break
            attempt += 1
            delay = wait if wait is not None else _backoff_delay(attempt)
            log.warning("%s %s -> %d; retry %d/%d in %.1fs",
                        method, path, r.status_code, attempt, retries, delay)
            await _retry_sleep(delay)
            continue
        break
    data = _parse(r)
    await caching._cache_call(caching.cache.put, key, data, ttl)
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
        detail = json.loads(body)
    except ValueError:  # JSONDecodeError: non-JSON error body
        detail = body[:300]
    return HTTPException(status, {"error": True, "code": status, "detail": detail})


# ---------------------------------------------------------------------------
# Auth — single account via env vars (or a saved CLI session), auto-login +
# auto-refresh
# ---------------------------------------------------------------------------
# The MDL app API requires a Bearer token for account features (title detail,
# watchlist, reviews, …). This wrapper logs in once with the account from the
# env and transparently refreshes the token when it expires (on HTTP 401 /
# invalid_grant), so every request can be authenticated. 2FA is NOT supported
# here — if your account has 2FA enabled, either disable it or use an app
# password.
MDL_USERNAME = os.environ.get("MDL_USERNAME", "").strip()
MDL_PASSWORD = os.environ.get("MDL_PASSWORD", "").strip()

_auth = {
    "token": None,
    "refresh_token": None,
    "device_id": str(uuid.uuid4()),
    "user": None,
    "username": None,
    "expires_at": 0.0,
    "login_error": None,
    "last_login": None,
    "last_refresh": None,
    "refreshes": 0,
}
_ANY = object()  # refresh() sentinel: refresh unconditionally


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _has_credentials() -> bool:
    return bool(MDL_USERNAME and MDL_PASSWORD)


def auth_enabled() -> bool:
    """True when credentials are configured or a saved session is loaded."""
    return _has_credentials() or bool(_auth["token"] or _auth["refresh_token"])


async def _auth_request(method: str, path: str, body: dict | None = None,
                        token: str | None = None) -> dict:
    """Direct upstream call for auth endpoints (bypasses cache; no auth header
    unless token passed). Returns parsed JSON or raises HTTPException."""
    data = _parse(await _send(method, path, default_headers(token=token), body))
    if not isinstance(data, dict):
        raise HTTPException(502, {"error": True, "code": 502,
                                  "detail": "unexpected auth response from upstream"})
    return data


async def login(force: bool = False) -> dict:
    """Login with configured credentials. Cached until forced or token expires."""
    async with _state().auth_lock:
        if (not force and _auth["token"]
                and _auth["expires_at"] > time.time()):
            return _auth
        return await _login_locked()


async def _login_locked() -> dict:
    """Login body; caller must hold _state().auth_lock (asyncio.Lock is not
    reentrant, so refresh() calls this instead of login())."""
    if not _has_credentials():
        if _auth["token"] or _auth["refresh_token"]:
            raise HTTPException(401, {"error": True, "code": 401,
                                      "detail": "saved session expired and could not be "
                                                "refreshed — run `mdlaw auth` again"})
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "MDL_USERNAME/MDL_PASSWORD not set"})
    log.info("logging in as %s", MDL_USERNAME)
    try:
        data = await _auth_request(
            "POST", _qs("/auth/login", device_id=_auth["device_id"]),
            body={"username": MDL_USERNAME, "password": _md5(MDL_PASSWORD)})
        # A successful login always carries an access token. Some responses
        # also include a `challenge_id` — that is a device-verification
        # challenge, NOT 2FA (2FA on MDL is a separate /auth/2fa/* flow).
        # Checking challenge_id first falsely rejected valid logins.
        if data.get("access_token") or data.get("token"):
            _set_tokens(data)
            _auth["username"] = MDL_USERNAME
            _auth["login_error"] = None
            _auth["last_login"] = time.time()
            log.info("login ok; token valid for %s", _fmt_duration(_auth["expires_at"] - time.time()))
            _save_auth()
            return _auth
        # No token: distinguish real 2FA (distinct response keys / code 428)
        # from other failures.
        if any(k in data for k in
               ("two_factor", "requires_2fa", "otp_required", "otp_challenge")) \
                or data.get("code") == 428:
            raise HTTPException(428, {"error": True, "code": 428,
                                      "detail": "2FA required on this account — "
                                                "disable 2FA or use an app password"})
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "login response missing token"})
    except HTTPException as e:
        _auth["login_error"] = str(e.detail)
        _auth["last_login"] = None
        raise


def _set_tokens(data: dict) -> None:
    _auth["token"] = data.get("access_token") or data.get("token")
    # A refresh response may omit these — keep the previous values then.
    _auth["refresh_token"] = data.get("refresh_token") or _auth["refresh_token"]
    _auth["user"] = data.get("user") or _auth["user"]
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
    # Not a decodable JWT (no token, no dots, bad base64/JSON, non-dict
    # payload, non-numeric exp): fall back to a 6h lifetime.
    except (AttributeError, IndexError, TypeError, ValueError):
        _auth["expires_at"] = time.time() + 6 * 3600


async def refresh(stale_token: object = _ANY) -> dict:
    """Refresh token; if refresh fails, re-login. Returns _auth dict.

    ``stale_token`` is the token the caller saw expire. If another request
    already replaced it while we waited for the lock, nothing is done — so a
    burst of concurrent 401s triggers one refresh, not one each.
    """
    async with _state().auth_lock:
        if (stale_token is not _ANY and _auth["token"]
                and _auth["token"] != stale_token
                and _auth["expires_at"] > time.time() + 60):
            return _auth
        if not _auth["refresh_token"]:
            return await _login_locked()
        log.info("refreshing access token")
        try:
            data = await _auth_request(
                "POST", _qs("/auth/refresh", device_id=_auth["device_id"]),
                body={"refresh_token": _auth["refresh_token"]},
                token=_auth["token"])
            if not (data.get("access_token") or data.get("token")):
                raise HTTPException(401, {"error": True, "code": 401,
                                          "detail": "refresh response missing token"})
            _set_tokens(data)
            _auth["last_refresh"] = time.time()
            _auth["refreshes"] += 1
            log.info("token refreshed; valid for %s", _fmt_duration(_auth["expires_at"] - time.time()))
            _save_auth()
        except HTTPException as e:
            if _has_credentials():
                log.warning("token refresh failed (HTTP %d); logging in again", e.status_code)
            else:
                log.info("token refresh failed (HTTP %d)", e.status_code)
            return await _login_locked()
        return _auth


async def auth_headers() -> dict[str, str]:
    """Headers with a valid Bearer token; auto-login/refresh when needed."""
    if not auth_enabled():
        return default_headers()
    if not _auth["token"] or _auth["expires_at"] <= time.time() + 60:
        if _auth["refresh_token"]:
            await refresh(stale_token=_auth["token"])
        else:
            await login()
    return default_headers(token=_auth["token"])


# Saved CLI session — `mdlaw auth` stores the token so later commands reuse it.
# Only the CLI (or a process that loaded that file) writes it: the HTTP server
# never puts tokens on disk (and Vercel's filesystem is read-only anyway).
_AUTH_FILE = os.path.join(os.path.expanduser("~"), ".mdlaw_auth.json")
_PERSIST_AUTH = False


def _save_auth() -> None:
    """Persist the current session to ~/.mdlaw_auth.json (mode 600). No-op
    unless session persistence is on; a write failure only warns."""
    if not _PERSIST_AUTH:
        return
    data = {
        "token": _auth["token"],
        "refresh_token": _auth["refresh_token"],
        "device_id": _auth["device_id"],
        "expires_at": _auth["expires_at"],
        "user": _auth["user"],
        "username": _auth["username"] or MDL_USERNAME,
    }
    try:
        fd = os.open(_AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            os.chmod(_AUTH_FILE, 0o600)
        except OSError:
            pass
    except OSError as e:
        log.warning("could not save session to %s: %s", _AUTH_FILE, e)


def _fmt_duration(seconds: float) -> str:
    """Human duration: '3d 4h', '5h 12m', '42m', 'under a minute'."""
    s = int(abs(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m" if m else "under a minute"


def _load_auth() -> bool:
    """Load a saved CLI session; returns True if a session was restored.
    The saved token/refresh_token drive requests (auth_enabled() is True), and
    refreshed tokens are written back to the same file."""
    global _PERSIST_AUTH
    try:
        with open(_AUTH_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    _auth.update({k: data[k] for k in
                  ("token", "refresh_token", "device_id", "expires_at", "user")
                  if k in data})
    if "username" in data:
        _auth["username"] = data["username"]
    _PERSIST_AUTH = True
    return True


# Server-side browse: POST /search/titles accepts filter params (recovered
# from the app's strings, ids verified live 2026-11-06). No `q` needed.
BROWSE_TYPES = {"drama": 68, "movie": 77, "tv-show": 86}
BROWSE_COUNTRIES = {"japan": 1, "china": 2, "south-korea": 3, "hong-kong": 4,
                    "taiwan": 5, "thailand": 6}
BROWSE_SORTS = ("top", "popular")
_BROWSE_ALIASES = {"korea": "south-korea", "kr": "south-korea", "jp": "japan", "cn": "china",
                   "hk": "hong-kong", "tw": "taiwan", "th": "thailand", "tv": "tv-show",
                   "show": "tv-show", "tvshow": "tv-show", "film": "movie", "films": "movie",
                   "dramas": "drama", "movies": "movie"}


def _slug(text: object) -> str:
    return "-".join(str(text).strip().lower().replace("_", " ").split())


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(400, {"error": True, "code": 400, "detail": detail})


def _as_list(values: object) -> list[str]:
    """None | 'a,b' | ['a', 3] → ['a', 'b', '3']"""
    if values is None:
        return []
    items = values if isinstance(values, (list, tuple, set)) else [values]
    return [part.strip() for v in items for part in str(v).split(",") if part.strip()]


def _filter_ids(values: object, table: dict[str, int], kind: str) -> list[int]:
    out = []
    for v in _as_list(values):
        if v.isdigit():
            out.append(int(v))
            continue
        key = _BROWSE_ALIASES.get(_slug(v), _slug(v))
        if key not in table:
            raise _bad_request(f"unknown {kind} {v!r}; use one of: {', '.join(table)} "
                               "(or a numeric id)")
        out.append(table[key])
    return out


async def _genre_ids(values: object) -> list[int]:
    """Genre names/slugs/ids → ids, resolved against GET /genres (cached 1h)."""
    vals = _as_list(values)
    if all(v.isdigit() for v in vals):
        return [int(v) for v in vals]
    catalog = await fetch("GET", "/genres", ttl=3600)
    by_name: dict[str, int] = {}
    for g in catalog if isinstance(catalog, list) else []:
        if isinstance(g, dict) and isinstance(g.get("id"), int):
            for k in (g.get("name"), g.get("slug")):
                if k:
                    by_name[_slug(k)] = g["id"]
    out = []
    for v in vals:
        gid = int(v) if v.isdigit() else by_name.get(_slug(v))
        if gid is None:
            raise _bad_request(f"unknown genre {v!r}; see /api/v1/genres for names and ids")
        out.append(gid)
    return out


async def browse_path(*, types: object = None, genres: object = None, countries: object = None,
                      released_from: int | None = None, sort: str = "top", page: int = 1,
                      q: str | None = None) -> str:
    """Build the upstream POST /search/titles path for a filtered browse.

    ``types``/``countries`` take names (``drama``, ``south-korea``) or ids;
    ``genres`` takes names, slugs or ids. Lists or comma-separated strings.
    ``released_from`` keeps titles released in that year or later.
    """
    if sort not in BROWSE_SORTS:
        raise _bad_request(f"sort must be one of: {', '.join(BROWSE_SORTS)}")
    params: dict[str, object] = {"edge": 1}
    if q:
        params["q"] = q
    params["page"] = page
    if t := _filter_ids(types, BROWSE_TYPES, "type"):
        params["types"] = ",".join(map(str, t))
    if g := await _genre_ids(genres):
        params["genres"] = ",".join(map(str, g))
    if c := _filter_ids(countries, BROWSE_COUNTRIES, "country"):
        params["countries"] = ",".join(map(str, c))
    if released_from:
        params["releases"] = int(released_from)
    params["sort"] = sort
    return _qs("/search/titles", **params)
