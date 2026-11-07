#!/usr/bin/env python3
"""
mdlaw — blazing-fast MyDramaList API wrapper.

Sources data from the official MDL Android app API (app-api.mydramalist.com/v1),
reconstructed from reversing com.mydramalist.app v2.3.18.

Run:  uvicorn mdlaw:app --port 8000
Self-check:  python mdlaw.py self   (offline)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import string
import sys
import textwrap
import threading
import time
import unicodedata
import uuid
import weakref
from collections import OrderedDict
from urllib.parse import urlencode

import httpx
from typing import Annotated, Any
from fastapi import FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from contextlib import asynccontextmanager

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

# Transport. The real gate on the MDL API is Cloudflare bot protection, which
# fingerprints the TLS/HTTP2 handshake (JA3/JA4). Default is curl_cffi
# (browser/mobile TLS impersonation), which passes the challenge from
# flagged/datacenter IPs. Set MDL_TRANSPORT=httpx for a plain httpx TLS stack
# (lighter, no curl_cffi needed) — works from most residential IPs.
TRANSPORT = os.environ.get("MDL_TRANSPORT", "curl_cffi").strip().lower()

__version__ = "1.8.0"

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

    label = "memory"

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "backend": self.label,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else None,
            "entries": self.entries(),
        }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None


class TTLCache(CacheBackend):
    """In-memory TTL cache with LRU eviction. Bounded by ``max_entries``
    (MDL_CACHE_MAX_ENTRIES, default 10000): every distinct query/page is a new
    key, so an unbounded dict grows forever on a public server."""

    def __init__(self, max_entries: int | None = None) -> None:
        self._d: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.max_entries = max(1, max_entries if max_entries is not None
                               else _env_int("MDL_CACHE_MAX_ENTRIES", 10_000))

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
        self._d.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: object, ttl: float) -> None:
        self._d[key] = (time.monotonic() + ttl, value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_entries:
            self._d.popitem(last=False)  # least recently used

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

    # MySQL: `key` is a reserved word, and TEXT can't be a PRIMARY KEY without
    # a prefix length. VARCHAR(768) utf8mb4 = 3072 bytes, the InnoDB index max.
    _SCHEMA_MYSQL = """
    CREATE TABLE IF NOT EXISTS mdlaw_cache (
        `key`      VARCHAR(768) NOT NULL PRIMARY KEY,
        value      LONGTEXT NOT NULL,
        expires_at DOUBLE NOT NULL,
        hash       CHAR(64) NOT NULL,
        created_at DOUBLE NOT NULL,
        updated_at DOUBLE NOT NULL,
        changed    TINYINT NOT NULL DEFAULT 0
    ) CHARACTER SET utf8mb4"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn = None
        self._flavor = ("mysql" if url.startswith("mysql://")
                        else "postgres" if url.startswith(("postgresql://", "postgres://"))
                        else "sqlite")
        self.label = self._flavor
        self._ph = "%s" if self._flavor != "sqlite" else "?"
        self._k = "`key`" if self._flavor == "mysql" else "key"
        self._puts = 0
        # One connection shared across worker threads (fetch() runs cache ops
        # via asyncio.to_thread) — serialize access.
        self._lock = threading.RLock()

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
                database=u.path.lstrip("/") or "", charset="utf8mb4",
                autocommit=False)
        # postgres
        try:
            import psycopg
        except ImportError:
            raise RuntimeError("PostgreSQL cache backend requires: pip install 'psycopg[binary]'")
        return psycopg.connect(self.url)

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(self._SCHEMA_MYSQL if self._flavor == "mysql" else self._SCHEMA)
        self._conn.commit()
        cur.close()

    def _cursor(self):
        if self._conn is None:
            self._conn = self._connect()
            self._init_schema()
        try:
            return self._conn.cursor()
        except Exception:
            # server went away — reconnect once
            self._conn = self._connect()
            self._init_schema()
            return self._conn.cursor()

    # -- ops ---------------------------------------------------------------
    def get(self, key: str) -> object | None:
        with self._lock:
            cur = self._cursor()
            cur.execute(f"SELECT value, expires_at FROM mdlaw_cache WHERE {self._k} = {self._ph}",
                        (key,))
            row = cur.fetchone()
            cur.close()
            if row is None:
                self.misses += 1
                return None
            value, expires = row
            if expires <= time.time():
                # Expired = miss, but keep the row: put() compares the new
                # response's hash against it to set `changed`. Deleting it
                # here made change detection always report changed=0.
                self.misses += 1
                return None
            self.hits += 1
            return json.loads(value)

    # Rows expired longer than this are pruned (they're only kept for change
    # detection, which needs the previous value right after expiry).
    PRUNE_AFTER = 7 * 86400
    PRUNE_EVERY = 500  # puts between prunes

    def prune(self) -> int:
        """Delete rows that expired more than PRUNE_AFTER seconds ago."""
        with self._lock:
            cur = self._cursor()
            cur.execute(f"DELETE FROM mdlaw_cache WHERE expires_at < {self._ph}",
                        (time.time() - self.PRUNE_AFTER,))
            n = cur.rowcount
            self._conn.commit()
            cur.close()
            return n

    def put(self, key: str, value: object, ttl: float) -> None:
        h = _json_hash(value)
        now = time.time()
        expires = now + ttl
        value_json = json.dumps(value, ensure_ascii=False)
        k, ph = self._k, self._ph
        with self._lock:
            cur = self._cursor()
            # detect change: compare hash of stored row vs new one
            cur.execute(f"SELECT hash, created_at FROM mdlaw_cache WHERE {k} = {ph}", (key,))
            row = cur.fetchone()
            if row is None:
                created = updated = now
                changed = 0
            else:
                old_hash, created = row
                changed = 0 if old_hash == h else 1
                updated = now
            cols = f"({k},value,expires_at,hash,created_at,updated_at,changed)"
            vals = "(" + ",".join([ph] * 7) + ")"
            if self._flavor == "mysql":
                sql = (f"INSERT INTO mdlaw_cache {cols} VALUES {vals} "
                       "ON DUPLICATE KEY UPDATE value=VALUES(value), expires_at=VALUES(expires_at), "
                       "hash=VALUES(hash), updated_at=VALUES(updated_at), changed=VALUES(changed)")
            else:
                sql = (f"INSERT INTO mdlaw_cache {cols} VALUES {vals} "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                       "expires_at=excluded.expires_at, hash=excluded.hash, "
                       "updated_at=excluded.updated_at, changed=excluded.changed")
            cur.execute(sql, (key, value_json, expires, h, created, updated, changed))
            self._conn.commit()
            cur.close()
            self._puts += 1
            if self._puts % self.PRUNE_EVERY == 1:
                self.prune()

    def entries(self) -> int:
        with self._lock:
            cur = self._cursor()
            cur.execute("SELECT COUNT(*) FROM mdlaw_cache")
            n = cur.fetchone()[0]
            cur.close()
            return n


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


async def _cache_call(fn, *args):
    """Run a cache op. The in-memory cache is called inline; SQL backends do
    blocking I/O, so they run in a worker thread to keep the event loop free."""
    if isinstance(cache, SQLCache):
        return await asyncio.to_thread(fn, *args)
    return fn(*args)

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
        except Exception:
            pass
        st.cffi = None
    if st.httpx is not None and not st.httpx.is_closed:
        await st.httpx.aclose()
    st.httpx = None


class _UvicornStyleFormatter(logging.Formatter):
    """`INFO:     mdlaw: ...` — lines up with uvicorn's own log lines."""

    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname + ':':<10}mdlaw: {record.getMessage()}"


def _setup_server_logging() -> None:
    """Server log lines in uvicorn's style (``INFO:     mdlaw: ...``). Level
    from MDL_LOG_LEVEL (default INFO). Leaves an already-configured logger
    alone, so a host app's logging config wins."""
    if log.handlers:
        return
    name = os.environ.get("MDL_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        level = logging.INFO
    h = logging.StreamHandler()
    h.setFormatter(_UvicornStyleFormatter())
    log.addHandler(h)
    log.setLevel(level)
    log.propagate = False
    if not isinstance(logging.getLevelName(name), int):
        log.warning("MDL_LOG_LEVEL=%s is not a log level; using INFO", name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup/shutdown: set up logging (client is lazy); close the upstream
    client on shutdown so uvicorn/gunicorn can restart cleanly."""
    _setup_server_logging()
    log.info("transport=%s cache=%s auth=%s", TRANSPORT, cache.label,
             "on" if auth_enabled() else "off")
    yield
    await close_client()


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
    hit = await _cache_call(cache.get, key)
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
    await _cache_call(cache.put, key, data, ttl)
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
    except Exception:
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
    except Exception:
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


app = FastAPI(
    title="mdlaw — MyDramaList API Wrapper",
    version=__version__,
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

# Shared parameter validation: bad input fails fast with 422 instead of
# reaching the upstream (and its throttle). QUERY's max_length also keeps
# cache keys within the MySQL backend's VARCHAR(768) key column.
# (Annotated aliases: a shared Query()/Path() instance gets mutated per route.)
Page = Annotated[int, Query(ge=1, le=1000)]
Limit = Annotated[int, Query(ge=1, le=100)]
SearchQ = Annotated[str, Query(min_length=1, max_length=200)]
PosId = Annotated[int, Path(ge=1)]
UserRef = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$",
                              description="numeric user id or username")]

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

# Inbound protection (all optional, off by default):
#   MDL_ADMIN_TOKEN  — enables POST /api/v1/auth/{login,refresh}; callers must
#                      send it as `X-Admin-Token`. Unset → those routes 404.
#   MDL_RATE_LIMIT   — max requests per minute per client IP on /api/v1/*
#                      (health excluded). 0/unset → no limit.
#   MDL_TRUST_PROXY  — 1 → take the client IP from Fly-Client-IP /
#                      X-Forwarded-For (only behind a proxy you control).
MDL_ADMIN_TOKEN = os.environ.get("MDL_ADMIN_TOKEN", "").strip()
try:
    RATE_LIMIT = max(0, int(os.environ.get("MDL_RATE_LIMIT", "0").strip() or 0))
except ValueError:
    raise RuntimeError("MDL_RATE_LIMIT must be an integer (requests/minute)") from None
TRUST_PROXY = os.environ.get("MDL_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")
_rate: dict[str, tuple[int, int]] = {}  # ip -> (minute window, count)


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        ip = (request.headers.get("fly-client-ip")
              or request.headers.get("x-forwarded-for", "").split(",")[0].strip())
        if ip:
            return ip
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    path = request.url.path
    if RATE_LIMIT and path.startswith("/api/v1/") and path != "/api/v1/health":
        now = time.time()
        window = int(now // 60)
        ip = _client_ip(request)
        w, n = _rate.get(ip, (window, 0))
        if w != window:
            w, n = window, 0
        if n >= RATE_LIMIT:
            return JSONResponse(
                {"detail": {"error": True, "code": 429, "detail": "rate limit exceeded"}},
                status_code=429, headers={"Retry-After": str(60 - int(now % 60))})
        _rate[ip] = (w, n + 1)
        if len(_rate) > 10_000:  # drop stale windows so the table can't grow unbounded
            for k in [k for k, (kw, _) in _rate.items() if kw != window]:
                del _rate[k]
    return await call_next(request)


def _require_admin(token: str | None) -> None:
    if not MDL_ADMIN_TOKEN:
        raise HTTPException(404, "Not Found")
    if not token or not secrets.compare_digest(token.encode(), MDL_ADMIN_TOKEN.encode()):
        raise HTTPException(403, {"error": True, "code": 403,
                                  "detail": "missing or invalid X-Admin-Token"})


# ---------------------------------------------------------------------------
# OpenAPI response schemas (docs only)
# ---------------------------------------------------------------------------
# Attached with `responses={...}`, never `response_model=`: routes still return
# the upstream JSON untouched. Upstream shapes belong to MDL and are only
# partly known, so every model allows extra fields and every field is optional.
# Fields listed here are the ones the code or tests actually read.
class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ErrorDetail(_Open):
    """Body that every HTTPException raised by mdlaw carries."""
    error: bool | None = None
    code: int | None = None
    detail: Any = None  # message string, or the upstream error JSON as-is


class ErrorResponse(_Open):
    """Error envelope: FastAPI wraps the HTTPException body under `detail`."""
    detail: ErrorDetail | None = None


class NamedRef(_Open):
    """Genre/tag entry inside title detail. Partial; upstream-defined."""
    id: int | None = None
    name: str | None = None
    slug: str | None = None


class TitleDetail(_Open):
    """GET /titles/{id}?expand=1. Partial; upstream-defined, extra fields pass through."""
    id: int | None = None
    title: str | None = None
    year: int | None = None
    episodes: int | None = None
    runtime: int | None = None
    rating: float | None = None
    votes: int | None = None
    ranked: int | None = None
    popularity: int | None = None  # 99999 means not ranked
    release_dates_fmt: str | None = None
    alt_titles: list[str] | None = None
    genres: list[NamedRef] | None = None
    tags: list[NamedRef] | None = None
    permalink: str | None = None
    country: str | None = None
    status: Any = None
    watchers: int | None = None
    certification: str | None = None


class TitleSummary(_Open):
    """One item of POST /search/titles (search and browse). Partial; upstream-defined.
    Search items carry no `genres` field."""
    id: int | None = None
    title: str | None = None
    year: int | None = None
    type: str | None = None
    media_type: str | None = None
    country: str | None = None
    language: str | None = None
    rating: float | None = None


class PersonDetail(_Open):
    """GET /people/{id}. Partial; upstream-defined, extra fields pass through."""
    id: int | None = None
    name: str | None = None
    original_name: str | None = None
    biography: str | None = None
    birthday: str | None = None
    dod: str | None = None  # "0000-00-00" when alive
    nationality: str | None = None
    gender: str | None = None
    flowers: int | None = None
    ranked: int | None = None
    permalink: str | None = None
    slug: str | None = None


class Health(_Open):
    """GET /api/v1/health (built by mdlaw)."""
    status: str | None = None
    service: str | None = None
    auth: bool | None = None


class CacheStats(_Open):
    """Cache backend stats (built by mdlaw)."""
    backend: str | None = None
    hits: int | None = None
    misses: int | None = None
    hit_rate: float | None = None
    entries: int | None = None


class AuthStatus(_Open):
    """GET /api/v1/auth/status (built by mdlaw). Never contains tokens."""
    configured: bool | None = None
    logged_in: bool | None = None
    user: Any = None
    expires_at: float | None = None
    last_login: float | None = None
    last_refresh: float | None = None
    refreshes: int | None = None
    login_error: str | None = None


class DashboardUpstream(_Open):
    base: str | None = None
    key_source: str | None = None
    transport: str | None = None


class DashboardAuth(_Open):
    configured: bool | None = None
    logged_in: bool | None = None
    refreshes: int | None = None


class DashboardRoute(_Open):
    path: str | None = None
    methods: list[str] | None = None


class Dashboard(_Open):
    """GET /api/v1/dashboard and /api/v1/status (built by mdlaw)."""
    status: str | None = None
    service: str | None = None
    version: str | None = None
    uptime_seconds: int | None = None
    cache: CacheStats | None = None
    upstream: DashboardUpstream | None = None
    auth: DashboardAuth | None = None
    routes: list[DashboardRoute] | None = None


# Every mdlaw error (bad input, auth, upstream status, network) uses ErrorResponse.
_ERRORS: dict = {"4XX": {"model": ErrorResponse, "description": "Client or upstream error"},
                 "5XX": {"model": ErrorResponse, "description": "Upstream unreachable or failed"}}


def _doc(model: object) -> dict:
    """`responses=` for a route: documented 200 body plus the shared error body."""
    return {200: {"model": model}, **_ERRORS}


@app.get("/", include_in_schema=False)
async def root():
    """Landing: point humans at the interactive docs."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


def _resp(data: object, ttl: float) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={int(ttl)}"})


@app.get("/api/v1/auth/status", responses=_doc(AuthStatus))
async def auth_status() -> dict:
    """Auth state: configured? logged in? token expiry, last refresh.
    Never includes the access/refresh tokens."""
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
async def auth_login(x_admin_token: str | None = Header(None)) -> dict:
    """Force a fresh login (credentials from env). Admin-only (MDL_ADMIN_TOKEN);
    returns auth status, never the tokens themselves."""
    _require_admin(x_admin_token)
    await login(force=True)
    return await auth_status()


@app.post("/api/v1/auth/refresh", include_in_schema=False)
async def auth_refresh(x_admin_token: str | None = Header(None)) -> dict:
    """Force a token refresh (or re-login if refresh fails). Admin-only."""
    _require_admin(x_admin_token)
    await refresh()
    return await auth_status()


@app.get("/api/v1/cache/stats", responses=_doc(CacheStats))
async def cache_stats() -> dict:
    """Cache backend info + hit stats."""
    return await _cache_call(cache.stats)


@app.get("/api/v1/health", responses={200: {"model": Health}})  # rate-limit exempt, never errors
async def health() -> dict:
    return {
        "status": "ok",
        "service": "mdlaw",
        "auth": auth_enabled(),
    }


@app.get("/api/v1/dashboard", responses=_doc(Dashboard))
async def dashboard() -> dict:
    """Health dashboard: uptime, cache stats, per-route registry."""
    return {
        "status": "ok",
        "service": "mdlaw",
        "version": __version__,
        "uptime_seconds": int(time.time() - _START),
        "cache": await _cache_call(cache.stats),
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


@app.get("/api/v1/status", responses=_doc(Dashboard))
async def status() -> JSONResponse:
    """Alias of /dashboard kept for monitoring probes."""
    return JSONResponse(await dashboard())


@app.get("/api/v1/genres")
async def genres() -> JSONResponse:
    return _resp(await fetch("GET", "/genres", ttl=3600), 3600)


@app.get("/api/v1/languages")
async def languages() -> JSONResponse:
    return _resp(await fetch("GET", "/languages/supported?v=2", ttl=3600), 3600)


@app.get("/api/v1/titles/trending")
async def titles_trending(page: Page = 1) -> JSONResponse:
    """Trending titles (requires auth)."""
    return _resp(await fetch("GET", f"/titles/trending?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/top_airing")
async def titles_top_airing(page: Page = 1) -> JSONResponse:
    """Top airing titles (requires auth)."""
    return _resp(await fetch("GET", f"/titles/top_airing?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/upcoming")
async def titles_upcoming(page: Page = 1) -> JSONResponse:
    """Upcoming titles (requires auth)."""
    return _resp(await fetch("GET", f"/titles/upcoming?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/currently_watching")
async def titles_currently_watching(page: Page = 1) -> JSONResponse:
    """Titles the community is currently watching (requires auth)."""
    return _resp(await fetch("GET", f"/titles/currently_watching?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/top_movies")
async def titles_top_movies(page: Page = 1) -> JSONResponse:
    """Top movies (requires auth)."""
    return _resp(await fetch("GET", f"/titles/top_movies?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/{tid}/reviews")
async def title_reviews(tid: PosId) -> JSONResponse:
    return _resp(await fetch("GET", f"/titles/{tid}/reviews", ttl=300), 300)


@app.get("/api/v1/titles/{tid}", responses=_doc(TitleDetail))
async def title_detail(tid: PosId) -> JSONResponse:
    """Title detail — requires auth (upstream 400s without a token)."""
    return _resp(await fetch("GET", f"/titles/{tid}?expand=1", ttl=300, auth=True), 300)


@app.get("/api/v1/titles/{tid}/recommendations")
async def title_recommendations(tid: PosId) -> JSONResponse:
    return _resp(await fetch("GET", f"/titles/{tid}/recommendations", ttl=600, auth=True), 600)


@app.post("/api/v1/search", responses=_doc(list[TitleSummary]))
async def search(q: SearchQ, page: Page = 1) -> JSONResponse:
    """Search titles by keyword (POST /search/titles?edge=1&q=...&page=...)."""
    if not q.strip():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "query param q is required"})
    return _resp(await fetch(
        "POST", _qs("/search/titles", edge=1, q=q, page=page, synopsis=1),
        ttl=300, auth=True), 300)


@app.post("/api/v1/search/people")
async def search_people(q: SearchQ, page: Page = 1) -> JSONResponse:
    """Search people by name (POST /search/people?q=...&page=...)."""
    if not q.strip():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "query param q is required"})
    return _resp(await fetch(
        "POST", _qs("/search/people", q=q, page=page),
        ttl=300, auth=True), 300)


@app.get("/api/v1/browse", responses=_doc(list[TitleSummary]))
async def browse(
    kind: Annotated[list[str] | None, Query(alias="type", description="drama | movie | tv-show (or id); repeatable")] = None,
    genre: Annotated[list[str] | None, Query(description="genre name or id; repeatable")] = None,
    country: Annotated[list[str] | None, Query(description="japan | china | south-korea | hong-kong | taiwan | thailand (or id)")] = None,
    released_from: Annotated[int | None, Query(alias="from", ge=1900, le=2100, description="released in this year or later")] = None,
    sort: Annotated[str, Query(pattern="^(top|popular)$")] = "top",
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Page = 1,
) -> JSONResponse:
    """Browse the catalog with server-side filters (requires auth).
    Upstream: POST /search/titles?edge=1&types=&genres=&countries=&releases=&sort="""
    path = await browse_path(types=kind, genres=genre, countries=country,
                             released_from=released_from, sort=sort, page=page, q=q)
    return _resp(await fetch("POST", path, ttl=600, auth=True), 600)


@app.post("/api/v1/search/users")
async def search_users(q: SearchQ) -> JSONResponse:
    """Search MDL users by name (requires auth)."""
    return _resp(await fetch("POST", _qs("/search/users", q=q), ttl=300, auth=True), 300)


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
async def title_comments(tid: PosId) -> JSONResponse:
    return _resp(await fetch("GET", f"/titles/{tid}/comments", ttl=300), 300)


@app.get("/api/v1/titles/{tid}/credits")
async def title_credits(tid: PosId) -> JSONResponse:
    # Auth-gated upstream (400 without token); same clear error as other
    # auth-gated endpoints.
    return _resp(await fetch("GET", f"/titles/{tid}/credits", ttl=300, auth=True), 300)


@app.get("/api/v1/calendar")
async def calendar() -> JSONResponse:
    return _resp(await fetch("POST", "/calendar/episodes", body={}, ttl=3600), 3600)


@app.get("/api/v1/articles/featured")
async def articles_featured(page: Page = 1) -> JSONResponse:
    return _resp(await fetch("GET", f"/articles/featured?page={page}", ttl=600), 600)


@app.get("/api/v1/lists/featured")
async def lists_featured(limit: Limit = 5) -> JSONResponse:
    return _resp(await fetch("GET", f"/lists/featured?limit={limit}", ttl=600), 600)


@app.get("/api/v1/lists/popular")
async def lists_popular(limit: Limit = 5) -> JSONResponse:
    return _resp(await fetch("GET", f"/lists/popular_voting_lists?limit={limit}", ttl=600), 600)


@app.get("/api/v1/lists/trending")
async def lists_trending(limit: Limit = 5) -> JSONResponse:
    """Trending user lists."""
    return _resp(await fetch("GET", f"/lists/trending?limit={limit}", ttl=600), 600)


@app.get("/api/v1/lists/recent")
async def lists_recent(limit: Limit = 5) -> JSONResponse:
    """Lists with recent activity."""
    return _resp(await fetch("GET", f"/lists/recent_activity?limit={limit}", ttl=600), 600)


@app.get("/api/v1/people/popular")
async def people_popular(page: Page = 1) -> JSONResponse:
    """Most popular people, 50 per page (requires auth)."""
    return _resp(await fetch("POST", _qs("/search/people", page=page, limit=50, sort="popular"),
                             ttl=3600, auth=True), 3600)


@app.get("/api/v1/people/leaderboard")
async def leaderboard(period: str = "alltime") -> JSONResponse:
    if period not in ("alltime", "weekly", "monthly"):
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "period must be alltime|weekly|monthly"})
    return _resp(await fetch("GET", f"/people/leaderboard?time_period={period}", ttl=600), 600)


@app.get("/api/v1/people/{pid}", responses=_doc(PersonDetail))
async def people(pid: PosId) -> JSONResponse:
    return _resp(await fetch("GET", f"/people/{pid}", ttl=86400), 86400)


@app.get("/api/v1/people/{pid}/credits")
async def people_credits(pid: PosId) -> JSONResponse:
    """Filmography (cast/crew) for a person/actor."""
    return _resp(await fetch("GET", f"/people/{pid}/credits", ttl=86400), 86400)


@app.get("/api/v1/users/{uid}")
async def user(uid: PosId) -> JSONResponse:
    """Public user profile by id."""
    return _resp(await fetch("GET", f"/users/{uid}", ttl=600), 600)


@app.get("/api/v1/users/{uid}/stats")
async def user_stats(uid: PosId) -> JSONResponse:
    """Public user stats (collected/rated counts, distribution)."""
    return _resp(await fetch("GET", f"/users/{uid}/stats", ttl=600), 600)


@app.get("/api/v1/users/{user}/lists")
async def user_lists(user: UserRef) -> JSONResponse:
    """A user's custom lists (requires auth). ``user`` = id or username."""
    return _resp(await fetch("GET", f"/users/{user}/lists", ttl=600, auth=True), 600)


@app.get("/api/v1/users/{user}/friends")
async def user_friends(user: UserRef) -> JSONResponse:
    """A user's friends (requires auth). ``user`` = id or username."""
    return _resp(await fetch("GET", f"/users/{user}/friends", ttl=600, auth=True), 600)


@app.get("/api/v1/recommendations")
async def recommendations(page: Page = 1) -> JSONResponse:
    """Community recommendations: "if you liked X, watch Y", with the reason."""
    return _resp(await fetch("GET", f"/recommendations?page={page}", ttl=600), 600)


@app.get("/api/v1/titles/{tid}/feed")
async def title_feed(tid: PosId) -> JSONResponse:
    """Community activity about a title (fetched anonymously)."""
    return _resp(await fetch("GET", f"/titles/{tid}/feed", ttl=300), 300)


@app.get("/api/v1/people/{pid}/feed")
async def people_feed(pid: PosId) -> JSONResponse:
    """Community activity about a person (fetched anonymously)."""
    return _resp(await fetch("GET", f"/people/{pid}/feed", ttl=300), 300)


@app.get("/api/v1/people/{pid}/gifts")
async def people_gifts(pid: PosId) -> JSONResponse:
    """Flowers sent to a person: top and most recent givers."""
    return _resp(await fetch("GET", f"/people/{pid}/gifts", ttl=600), 600)


@app.get("/api/v1/articles/categories")
async def articles_categories() -> JSONResponse:
    """Article categories (news, interviews, …)."""
    return _resp(await fetch("GET", "/articles/categories", ttl=86400), 86400)


@app.get("/api/v1/groups")
async def groups(page: Page = 1) -> JSONResponse:
    """Community groups, 20 per page."""
    return _resp(await fetch("GET", f"/groups?page={page}", ttl=3600), 3600)


@app.get("/api/v1/groups/{gid}")
async def group(gid: PosId) -> JSONResponse:
    """A community group."""
    return _resp(await fetch("GET", f"/groups/{gid}", ttl=3600), 3600)


@app.get("/api/v1/reviews/{rid}")
async def review(rid: PosId) -> JSONResponse:
    """A single review: ratings breakdown, text, author, title."""
    return _resp(await fetch("GET", f"/reviews/{rid}", ttl=3600), 3600)


@app.get("/api/v1/articles/{aid}")
async def article(aid: PosId) -> JSONResponse:
    """A single article (news/feature) with its body text."""
    return _resp(await fetch("GET", f"/articles/{aid}", ttl=3600), 3600)


@app.get("/api/v1/tags/search")
async def search_tags(q: SearchQ) -> JSONResponse:
    """Search drama tags (e.g. q=romance → Romance, Bromance, …)."""
    return _resp(await fetch("GET", _qs("/tags/search", q=q), ttl=600), 600)


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
        return await self.get(_qs("/people/leaderboard", time_period=period), ttl=600)

    async def people(self, pid: int) -> object:
        return await self.get(f"/people/{pid}", ttl=86400)

    async def people_credits(self, pid: int) -> object:
        """Filmography (cast/crew) for a person/actor."""
        return await self.get(f"/people/{pid}/credits", ttl=86400)

    async def user(self, uid: int) -> object:
        """Public user profile by id."""
        return await self.get(f"/users/{uid}", ttl=600)

    async def user_stats(self, uid: int) -> object:
        """Public user stats (collected/rated counts, distribution)."""
        return await self.get(f"/users/{uid}/stats", ttl=600)

    async def search_tags(self, q: str) -> object:
        """Search drama tags (e.g. q=romance → Romance, Bromance, …)."""
        return await self.get(_qs("/tags/search", q=q), ttl=600)

    async def payment_plans(self) -> object:
        return await self.get("/payment/plans", ttl=3600)

    async def payment_coins(self) -> object:
        return await self.get("/payment/coins", ttl=3600)

    # --- titles ---
    async def titles_trending(self, page: int = 1) -> object:
        """Trending titles (requires auth)."""
        return await self.get(f"/titles/trending?page={page}", ttl=600, auth=True)

    async def titles_top_airing(self, page: int = 1) -> object:
        """Top airing titles (requires auth)."""
        return await self.get(f"/titles/top_airing?page={page}", ttl=600, auth=True)

    async def titles_upcoming(self, page: int = 1) -> object:
        """Upcoming titles (requires auth)."""
        return await self.get(f"/titles/upcoming?page={page}", ttl=600, auth=True)

    async def titles_currently_watching(self, page: int = 1) -> object:
        """Titles the community is currently watching (requires auth)."""
        return await self.get(f"/titles/currently_watching?page={page}", ttl=600, auth=True)

    async def titles_top_movies(self, page: int = 1) -> object:
        """Top movies (requires auth)."""
        return await self.get(f"/titles/top_movies?page={page}", ttl=600, auth=True)

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
    # Upstream search is POST /search/titles?edge=1&q=<q>&page=<page>&synopsis=1.
    # `q` and `page` are URL query params, NOT a JSON body. Search items carry
    # country/language/type/media_type/year but no genres field; post-filters
    # below run in Python. Genre filtering: use browse_by_genre().
    async def search(self, q: str = "", country: str | None = None,
                     language: str | None = None, type: str | None = None,
                     media_type: str | None = None, year: int | None = None,
                     limit: int | None = None, page: int = 1) -> object:
        """Search titles (POST /search/titles?edge=1&q=...&page=...).

        ``q`` is passed upstream as a URL query param and IS honored (verified
        live: ``q="crash landing on you"`` → Crash Landing on You; ``page=2``
        returns different results). ``country``/``language``/``type``/
        ``media_type``/``year``/``limit`` are client-side post-filters over the
        fields search results carry. Genre is NOT filterable here (search items
        have no genres field) — use :meth:`browse_by_genre` instead.
        """
        results = await self.post(_qs("/search/titles", edge=1, q=q, page=page, synopsis=1),
                                  {}, ttl=300, auth=True)
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

    async def browse(self, types: object = None, genres: object = None,
                     countries: object = None, released_from: int | None = None,
                     sort: str = "top", page: int = 1, q: str | None = None) -> object:
        """Browse the catalog with **server-side** filters (requires auth).

        ``types``: ``"drama"``, ``"movie"``, ``"tv-show"`` (or ids 68/77/86);
        ``genres``: names, slugs or ids from :meth:`genres`; ``countries``:
        ``"japan"``, ``"china"``, ``"south-korea"``, ``"hong-kong"``,
        ``"taiwan"``, ``"thailand"`` (or ids 1–6). Each accepts a single value
        or a list. ``released_from``: year (inclusive). ``sort``: ``"top"``
        (by rating) or ``"popular"``. 20 results per page.

            await mdl.browse(types="drama", genres="romance",
                             countries="south-korea", sort="popular")
        """
        path = await browse_path(types=types, genres=genres, countries=countries,
                                 released_from=released_from, sort=sort, page=page, q=q)
        return await self.post(path, {}, ttl=600, auth=True)

    async def browse_by_genre(self, genre_id: int, limit: int = 10,
                              source: str = "search") -> list:
        """Titles in ``genre_id``.

        ``source="search"`` (default) uses the server-side genre filter — one
        request, returns search items (no ``genres`` field). ``"trending"`` /
        ``"top_movies"`` keep the old behaviour: fetch each candidate's detail
        (one auth-gated call per title) and return the matching details.
        """
        if source == "search":
            items = await self.browse(genres=genre_id)
            return items[:limit] if isinstance(items, list) else []
        if source == "trending":
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

    async def people_popular(self, page: int = 1) -> object:
        """Most popular people, 50 per page (requires auth)."""
        return await self.post(_qs("/search/people", page=page, limit=50, sort="popular"), {},
                               ttl=3600, auth=True)

    async def search_users(self, q: str) -> object:
        """Search MDL users by name (requires auth)."""
        return await self.post(_qs("/search/users", q=q), {}, ttl=300, auth=True)

    async def lists_trending(self, limit: int = 5) -> object:
        return await self.get(f"/lists/trending?limit={limit}", ttl=600)

    async def lists_recent(self, limit: int = 5) -> object:
        """Lists with recent activity."""
        return await self.get(f"/lists/recent_activity?limit={limit}", ttl=600)

    async def user_lists(self, user: int | str) -> object:
        """A user's custom lists (id or username; requires auth)."""
        return await self.get(f"/users/{user}/lists", ttl=600, auth=True)

    async def user_friends(self, user: int | str) -> object:
        """A user's friends (id or username; requires auth)."""
        return await self.get(f"/users/{user}/friends", ttl=600, auth=True)

    async def recommendations(self, page: int = 1) -> object:
        """Community recommendations: "if you liked X, watch Y", with the reason."""
        return await self.get(f"/recommendations?page={page}", ttl=600)

    async def title_feed(self, tid: int) -> object:
        """Community activity about a title."""
        return await self.get(f"/titles/{tid}/feed", ttl=300)

    async def people_feed(self, pid: int) -> object:
        """Community activity about a person."""
        return await self.get(f"/people/{pid}/feed", ttl=300)

    async def people_gifts(self, pid: int) -> object:
        """Flowers sent to a person: ``top`` and ``recents`` givers."""
        return await self.get(f"/people/{pid}/gifts", ttl=600)

    async def articles_categories(self) -> object:
        return await self.get("/articles/categories", ttl=86400)

    async def groups(self, page: int = 1) -> object:
        """Community groups, 20 per page (``{"items": [...]}``)."""
        return await self.get(f"/groups?page={page}", ttl=3600)

    async def group(self, gid: int) -> object:
        return await self.get(f"/groups/{gid}", ttl=3600)

    async def review(self, rid: int) -> object:
        """A single review: ratings breakdown, text, author, title."""
        return await self.get(f"/reviews/{rid}", ttl=3600)

    async def article(self, aid: int) -> object:
        """A single article with its body text."""
        return await self.get(f"/articles/{aid}", ttl=3600)

    async def search_people(self, q: str, page: int = 1) -> object:
        return await self.post(_qs("/search/people", q=q, page=page), {},
                               ttl=300, auth=True)

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# Output: readable tables/cards on a terminal, raw JSON when piped (so
# `mdlaw genres | jq` keeps working) — override with --json / -o text.
# Errors: one "error:" line plus a "hint:" with the next step; exit codes
# 0 ok · 1 API/runtime error · 2 bad usage · 130 interrupted.
# Logs: -v shows each upstream request (status, time, size) and auth events;
# -vv adds cache hits/misses, throttle waits and tracebacks.

_EPILOG = """\
examples:
  mdlaw auth                        log in once (prompts for the password)
  mdlaw search "crash landing"      search titles
  mdlaw title 686                   title detail
  mdlaw people 14352                actor profile
  mdlaw browse --type drama --genre romance --country korea --sort popular
  mdlaw recs                        community picks: "liked X → try Y"
  mdlaw calendar --days 3           episodes airing in the next 3 days
  mdlaw leaderboard weekly -n 10    top 10 actors this week
  mdlaw genres --json | jq '.[].name'
  mdlaw -v me                       show the requests being made

Output is a table on a terminal and JSON when piped; force either with
--json or -o text. Commands marked * need an account (mdlaw auth).
"""

_WATCH_STATUSES = ("completed", "dropped", "onhold", "plantowatch",
                   "notinterested", "undecided")


class _CLIError(Exception):
    """A user-facing failure: message + optional hint, no traceback."""

    def __init__(self, message: str, hint: str | None = None, code: int = 1) -> None:
        super().__init__(message)
        self.message, self.hint, self.code = message, hint, code


# -- argument parsing -------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """argparse with mdlaw-style errors: `error:` + `hint:`, a short usage
    line, and a did-you-mean for mistyped commands."""

    def error(self, message: str) -> None:  # type: ignore[override]
        import difflib
        import re
        m = re.search(r"argument <command>: invalid choice: '([^']*)'", message)
        if m:
            choices = [c for c in self._commands()]
            near = difflib.get_close_matches(m.group(1), choices, n=1)
            _print_error(f"unknown command '{m.group(1)}'",
                         f"did you mean `mdlaw {near[0]}`?" if near
                         else "run `mdlaw --help` to see all commands")
        else:
            print(self.format_usage().rstrip(), file=sys.stderr)
            _print_error(message, f"see `{self.prog} --help`")
        sys.exit(2)

    def _commands(self) -> list[str]:
        for a in self._actions:
            if isinstance(a, argparse._SubParsersAction):
                return list(a.choices)
        return []


def _short_usage(p: argparse.ArgumentParser) -> str:
    """`mdlaw title [options] id` — leave the global options to --help."""
    parts = []
    for a in p._actions:
        if a.option_strings or isinstance(a, argparse._SubParsersAction):
            continue
        name = a.metavar or a.dest
        if a.choices and not a.metavar:
            name = "{" + ",".join(a.choices) + "}"
        parts.append(f"[{name}]" if a.nargs == "?" else
                     f"{name} [{name} ...]" if a.nargs == "+" else name)
    return " ".join(["%(prog)s", "[options]"] + parts)


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {n}")
    return n


def _country(text: str) -> str:
    """Validate --country locally (name, alias or id) so typos are usage errors."""
    try:
        _filter_ids(text, BROWSE_COUNTRIES, "country")
    except HTTPException:
        raise argparse.ArgumentTypeError(
            f"unknown country {text!r} (choose from {', '.join(BROWSE_COUNTRIES)}, "
            "korea, jp, … or an id)") from None
    return text


def _limit(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 (all) or more")
    return n


def _add_output_options(p: argparse.ArgumentParser, sub: bool) -> None:
    """Global options, accepted before or after the command. On subparsers
    the defaults are SUPPRESSed so they don't overwrite the main parser's."""
    d = argparse.SUPPRESS if sub else None
    g = p.add_argument_group("output options")
    g.add_argument("-o", "--output", choices=("auto", "text", "json"),
                   default=d if sub else "auto",
                   help="auto (default): text on a terminal, JSON when piped")
    g.add_argument("--json", dest="output", action="store_const", const="json",
                   default=argparse.SUPPRESS, help="shorthand for -o json")
    g.add_argument("-n", "--limit", type=_limit, default=d,
                   help="max rows in text output (0 = all)")
    g.add_argument("-v", "--verbose", action="count", default=d if sub else 0,
                   help="-v: log requests and auth; -vv: also cache/throttle and tracebacks")
    g.add_argument("--transport", choices=("curl_cffi", "httpx"), default=d,
                   help="upstream HTTP stack (default: MDL_TRANSPORT or curl_cffi)")


def _build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    parser = _Parser(
        prog="mdlaw", formatter_class=fmt, epilog=_EPILOG,
        description="MyDramaList from the command line (unofficial; uses the MDL app API).")
    parser.add_argument("--version", action="version", version=f"mdlaw {__version__}")
    _add_output_options(parser, sub=False)
    sp = parser.add_subparsers(dest="command", metavar="<command>")

    def cmd(name: str, help_: str, **kw) -> argparse.ArgumentParser:
        desc = (help_[2:] + " — needs a MyDramaList account (log in with `mdlaw auth`)"
                if help_.startswith("* ") else help_)
        p = sp.add_parser(name, help=help_, description=desc, formatter_class=fmt, **kw)
        _add_output_options(p, sub=True)
        made.append(p)
        return p
    made: list[argparse.ArgumentParser] = []

    p = cmd("auth", "log in and save the session, or show it (auth status)")
    p.add_argument("username", nargs="?",
                   help="MDL username or email; `status` shows the saved session")
    p.add_argument("password", nargs="?",
                   help="password (avoid: it ends up in shell history — use the prompt)")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin (for scripts)")
    cmd("logout", "remove the saved session")
    cmd("genres", "list all genres")
    cmd("languages", "list supported languages")
    p = cmd("calendar", "upcoming episodes and releases")
    p.add_argument("--days", type=_positive_int, default=7,
                   help="how many days ahead to show in text output (default 7)")
    p = cmd("search", "* search titles")
    p.add_argument("query", nargs="+")
    p.add_argument("--page", type=_positive_int, default=1)
    p.add_argument("--country", help="keep only this country, e.g. 'South Korea'")
    p.add_argument("--type", help="keep only this type, e.g. Drama or Movie")
    p.add_argument("--year", type=_positive_int, help="keep only this release year")
    p = cmd("browse", "* browse the catalog with filters (type, genre, country, year)")
    p.add_argument("--type", dest="types", action="append", choices=list(BROWSE_TYPES),
                   help="repeatable")
    p.add_argument("--genre", dest="genres", action="append", metavar="GENRE",
                   help="genre name or id, e.g. romance; repeatable (see `mdlaw genres`)")
    p.add_argument("--country", dest="countries", action="append", metavar="COUNTRY", type=_country,
                   help=f"{' | '.join(BROWSE_COUNTRIES)}; repeatable")
    p.add_argument("--from", dest="released_from", type=_positive_int, metavar="YEAR",
                   help="released in YEAR or later")
    p.add_argument("--sort", choices=BROWSE_SORTS, default="top",
                   help="top = highest rated (default), popular = most watched")
    p.add_argument("--page", type=_positive_int, default=1)
    p = cmd("recommendations", "community picks: \"if you liked X, watch Y\"", aliases=["recs"])
    p.add_argument("--page", type=_positive_int, default=1)
    p = cmd("lists", "user lists: trending (default), recent, popular, featured")
    p.add_argument("which", nargs="?", default="trending",
                   choices=("trending", "recent", "popular", "featured"))
    p = cmd("search-people", "* search actors and crew")
    p.add_argument("query", nargs="+")
    p.add_argument("--page", type=_positive_int, default=1)
    p = cmd("title", "* title detail")
    p.add_argument("id", type=_positive_int, help="title id (from search results)")
    p = cmd("people", "actor / crew profile")
    p.add_argument("id", type=_positive_int, help="person id")
    p = cmd("watchlist", "* your lists (default: what you're watching)")
    p.add_argument("status", nargs="?", choices=_WATCH_STATUSES,
                   help="which list; omit for the titles you're currently watching")
    cmd("me", "* your profile")
    p = cmd("leaderboard", "most popular actors")
    p.add_argument("period", nargs="?", default="alltime",
                   choices=("alltime", "weekly", "monthly"))
    cmd("self", "offline self-check")
    p = cmd("serve", "start the HTTP API server (default when no command is given)",
            aliases=["run"])
    p.add_argument("--host", help="bind address (default: HOST env or 0.0.0.0)")
    p.add_argument("--port", help="port (default: PORT env or 8000)")
    for p in made:
        p.usage = _short_usage(p)
    return parser


# -- terminal output helpers ------------------------------------------------
def _ensure_utf8_stdio() -> None:
    """Piped output on Windows defaults to the ANSI code page, which can't
    encode Korean/Chinese titles. JSON consumers expect UTF-8 anyway."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def _term_width() -> int:
    return max(40, shutil.get_terminal_size((100, 24)).columns)


def _dw(text: str) -> int:
    """Display width: CJK / full-width characters take two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else
               0 if unicodedata.combining(c) else 1 for c in text)


def _fit(text: str, width: int) -> str:
    """Truncate to ``width`` display columns (with …) and pad to it."""
    if _dw(text) > width:
        out, w = [], 0
        for c in text:
            cw = _dw(c)
            if w + cw > width - 1:
                break
            out.append(c)
            w += cw
        text = "".join(out) + "…"
    return text + " " * (width - _dw(text))


def _table(headers: list[str], rows: list[list[object]], flex: int = 1) -> list[str]:
    """Aligned columns; column ``flex`` shrinks to fit the terminal width."""
    cells = [[_clean(c) for c in row] for row in rows]
    widths = [max([_dw(h)] + [_dw(r[i]) for r in cells]) for i, h in enumerate(headers)]
    over = sum(widths) + 2 * (len(widths) - 1) - _term_width()
    if over > 0:
        widths[flex] = max(12, widths[flex] - over)
    gap = "  "
    lines = [gap.join(_fit(h, w) for h, w in zip(headers, widths)).rstrip(),
             gap.join("─" * w for w in widths)]
    lines += [gap.join(_fit(c, w) for c, w in zip(r, widths)).rstrip() for r in cells]
    return lines


def _card(title: str, fields: list[tuple[str, object]], body: str | None = None,
          subtitle: str | None = None) -> list[str]:
    """Heading, aligned label/value rows, then wrapped body text."""
    lines = [title]
    if subtitle:
        lines.append(subtitle)
    shown = [(k, _clean(v)) for k, v in fields if _clean(v)]
    if shown:
        lines.append("")
        lw = max(_dw(k) for k, _ in shown)
        vw = max(20, _term_width() - lw - 2)
        for k, v in shown:
            wrapped = textwrap.wrap(v, vw) or [""]
            lines.append(f"{k}{' ' * (lw - _dw(k))}  {wrapped[0]}")
            lines += [" " * (lw + 2) + w for w in wrapped[1:]]
    if body:
        lines.append("")
        for para in str(body).replace("\r", "").split("\n"):
            lines += textwrap.wrap(para, min(_term_width(), 100)) or [""]
    return lines


_EMPTY = (None, "", "0000-00-00", "0001-01-01", [], {})


def _clean(v: object) -> str:
    if v in _EMPTY:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return ", ".join(_clean(x.get("name") if isinstance(x, dict) else x) for x in v)
    return " ".join(str(v).split())


def _num(v: object) -> str | None:
    """Thousands separators for counts (not for ids)."""
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) and v else None


def _pick(d: dict, *keys: str) -> object:
    for k in keys:
        v = d.get(k)
        if v not in _EMPTY:
            return v
    return None


def _items(data: object, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys + ("items", "results", "entries", "data", "titles"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _limited(rows: list, limit: int | None, default: int | None) -> tuple[list, str | None]:
    n = default if limit is None else limit
    if not n or len(rows) <= n:
        return rows, None
    return rows[:n], f"showing {n} of {len(rows)} · -n 0 shows all"


def _mdl_url(slug: object) -> str | None:
    if not slug:
        return None
    s = str(slug)
    return s if s.startswith("http") else "https://mydramalist.com/" + s.lstrip("/")


def _rank(v: object) -> str | None:
    """'#11,994'; MDL uses 99999 for 'not ranked'."""
    return f"#{v:,}" if isinstance(v, int) and 0 < v < 99999 else None


def _year(d: dict) -> str:
    y = _pick(d, "year", "released_year")
    if y:
        return str(y)
    date = _pick(d, "released", "aired_start", "release_date")
    return str(date)[:4] if date else ""


def _rating(d: dict) -> str:
    r = _pick(d, "rating", "score")
    try:
        return f"{float(r):.1f}" if r and float(r) > 0 else ""
    except (TypeError, ValueError):
        return ""


# -- renderers: data → list of lines ----------------------------------------
def _render_genres(data: object, args) -> list[str]:
    rows = [[g.get("id"), g.get("name")] for g in _items(data) if isinstance(g, dict)]
    return _table(["ID", "Genre"], rows) + ["", f"{len(rows)} genres"]


def _render_languages(data: object, args) -> list[str]:
    langs = _items(data, "languages")
    rows = [[x.get("code"), x.get("name"), x.get("native_name")] for x in langs
            if isinstance(x, dict)]
    return _table(["Code", "Language", "Native name"], rows) + ["", f"{len(rows)} languages"]


def _render_calendar(data: object, args) -> list[str]:
    rels = {r.get("id"): r for r in _items(data, "relationships") if isinstance(r, dict)}
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))  # local midnight
    end = start + args.days * 86400
    items = sorted((i for i in _items(data) if isinstance(i, dict)
                    and start <= (i.get("released_at") or 0) < end),
                   key=lambda i: (i["released_at"],
                                  str((rels.get(i.get("rid")) or {}).get("title") or ""),
                                  i.get("episode_number") or 0))
    if not items:
        return [f"Nothing scheduled in the next {args.days} day(s)."]
    out: list[str] = []
    day = None
    for it in items:
        lt = time.localtime(it["released_at"])
        if lt[:3] != day:
            day = lt[:3]
            if out:
                out.append("")
            out.append(time.strftime("%A %d %B %Y", lt))
        rel = rels.get(it.get("rid")) or rels.get(it.get("id")) or {}
        ep = it.get("episode_number")
        what = f"ep {ep}" if ep else (rel.get("type") or "")
        line = f"  {time.strftime('%H:%M', lt)}  {_fit(_clean(rel.get('title') or it.get('permalink')), 40)}" \
               f"  {_fit(what, 8)}  {_clean(rel.get('country'))}"
        out.append(line.rstrip())
    out += ["", f"{len(items)} releases in the next {args.days} day(s) (local time) · "
                f"--days N to change"]
    return out


def _title_table(items: list) -> list[list[object]]:
    return [[i.get("id"), _pick(i, "title", "name"), _year(i),
              _pick(i, "media_type", "type", "content_type"), _pick(i, "country", "language"),
              _rating(i)] for i in items]


def _render_search(data: object, args) -> list[str]:
    items = [i for i in _items(data) if isinstance(i, dict)]
    if not items:
        return ["No titles found."]
    rows, more = _limited(_title_table(items), args.limit, None)
    q = " ".join(args.query)
    out = _table(["ID", "Title", "Year", "Type", "Country", "Rating"], rows)
    out += ["", f"page {args.page} · details: mdlaw title <ID> · next page: "
                f"mdlaw search {shlex.quote(q)} --page {args.page + 1}"]
    return out + ([more] if more else [])


def _browse_cmd(args, page: int) -> str:
    parts = ["mdlaw browse"]
    parts += [f"--type {t}" for t in args.types or []]
    parts += [f"--genre {shlex.quote(g)}" for g in args.genres or []]
    parts += [f"--country {shlex.quote(c)}" for c in args.countries or []]
    if args.released_from:
        parts.append(f"--from {args.released_from}")
    if args.sort != "top":
        parts.append(f"--sort {args.sort}")
    return " ".join(parts + [f"--page {page}"])


def _render_browse(data: object, args) -> list[str]:
    items = [i for i in _items(data) if isinstance(i, dict)]
    if not items:
        return ["No titles match these filters." + (" Try an earlier page." if args.page > 1 else "")]
    rows, more = _limited(_title_table(items), args.limit, None)
    out = _table(["ID", "Title", "Year", "Type", "Country", "Rating"], rows)
    out += ["", f"page {args.page} · sorted by {args.sort} · details: mdlaw title <ID>",
            f"next page: {_browse_cmd(args, args.page + 1)}"]
    return out + ([more] if more else [])


def _render_recommendations(data: object, args) -> list[str]:
    recs = [r for r in _items(data) if isinstance(r, dict)]
    if not recs:
        return ["No recommendations."]
    recs, more = _limited(recs, args.limit, 10)
    width = min(_term_width(), 100)
    out: list[str] = []
    for r in recs:
        a = r.get("title") if isinstance(r.get("title"), dict) else {}
        b = r.get("similar") if isinstance(r.get("similar"), dict) else {}

        def name(t: dict) -> str:
            y = _year(t)
            return f"{_clean(t.get('title'))}" + (f" ({y})" if y else "") + f" [{t.get('id')}]"
        out.append(f"Liked {name(a)}")
        out.append(f"  → try {name(b)}")
        reason = _clean(r.get("content"))
        if reason:
            out += textwrap.wrap(f'"{reason}"', width, initial_indent="    ",
                                 subsequent_indent="     ")[:4]
        who = _clean((r.get("author") or {}).get("display_name"))
        likes = _num(r.get("total_likes"))
        meta = " · ".join(x for x in (f"by {who}" if who else "", f"{likes} likes" if likes else "") if x)
        if meta:
            out.append(f"    {meta}")
        out.append("")
    out.append(f"page {args.page} · details: mdlaw title <ID> · next: "
               f"mdlaw recommendations --page {args.page + 1}")
    return out + ([more] if more else [])


def _render_lists(data: object, args) -> list[str]:
    items = [i for i in _items(data, "lists") if isinstance(i, dict)]
    if not items:
        return ["No lists."]
    rows = [[i.get("id"), _pick(i, "name", "title"), _num(i.get("count")) or _clean(i.get("count")),
             _num(i.get("total_likes")), _num(i.get("total_views"))] for i in items]
    rows, more = _limited(rows, args.limit, None)
    out = _table(["ID", "List", "Titles", "Likes", "Views"], rows)
    return out + ["", f"{args.which} lists · -n N for more"] + ([more] if more else [])


def _render_search_people(data: object, args) -> list[str]:
    items = [i for i in _items(data) if isinstance(i, dict)]
    if not items:
        return ["No people found."]
    rows = [[i.get("id"), _pick(i, "name", "display_name"),
             _pick(i, "original_name", "native_name"), _pick(i, "nationality", "country")]
            for i in items]
    rows, more = _limited(rows, args.limit, None)
    q = " ".join(args.query)
    out = _table(["ID", "Name", "Native name", "Nationality"], rows)
    out += ["", f"page {args.page} · profile: mdlaw people <ID> · next page: "
                f"mdlaw search-people {shlex.quote(q)} --page {args.page + 1}"]
    return out + ([more] if more else [])


def _render_title(data: object, args) -> list[str]:
    if not isinstance(data, dict):
        return _render_generic(data, args)
    name = _clean(_pick(data, "title", "name")) or f"title {args.id}"
    year = _year(data)
    genres = data.get("genres")
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",")]
    rating = _rating(data)
    votes = _pick(data, "votes", "rating_count")
    eps, runtime = data.get("episodes"), data.get("runtime")
    aired = _pick(data, "release_dates_fmt", "aired")
    if not aired and data.get("aired_start"):
        aired = " – ".join(str(x) for x in (data.get("aired_start"), data.get("aired_end")) if x)
    alt = data.get("alt_titles")
    fields = [
        ("Native title", _pick(data, "original_title", "native_title")),
        ("Also known as", ", ".join(map(str, alt[:3])) + (" …" if len(alt) > 3 else "")
         if isinstance(alt, list) and alt else None),
        ("Type", _pick(data, "media_type", "type", "content_type")),
        ("Country", data.get("country")),
        ("Episodes", (f"{eps}" + (f" × {runtime} min" if runtime else "")) if eps else None),
        ("Aired", aired),
        ("Status", data.get("status")),
        ("Rating", f"{rating}/10" + (f" ({_num(votes) or votes} votes)" if votes else "")
         if rating else None),
        ("Ranked", _rank(_pick(data, "ranked", "ranking"))),
        ("Popularity", _rank(data.get("popularity"))),
        ("Watchers", _num(data.get("watchers"))),
        ("Certification", data.get("certification")),
        ("Genres", genres),
        ("Tags", data.get("tags")),
        ("Link", _mdl_url(_pick(data, "permalink", "url", "slug"))),
    ]
    return _card(f"{name} ({year})" if year else name, fields,
                 body=_pick(data, "synopsis", "description"))


def _render_people(data: object, args) -> list[str]:
    if not isinstance(data, dict):
        return _render_generic(data, args)
    name = _clean(_pick(data, "name", "display_name")) or f"person {args.id}"
    native = _clean(data.get("original_name"))
    fields = [
        ("Nationality", data.get("nationality")),
        ("Gender", data.get("gender")),
        ("Born", data.get("birthday")),
        ("Died", data.get("dod")),
        ("Flowers", _num(data.get("flowers"))),
        ("Rank", f"#{data['ranked']}" if data.get("ranked") else None),
        ("Link", _mdl_url(data.get("slug")) or _mdl_url(data.get("permalink"))),
    ]
    return _card(f"{name} ({native})" if native else name, fields,
                 body=data.get("biography"))


def _render_leaderboard(data: object, args) -> list[str]:
    entries = [e for e in _items(data, "entries") if isinstance(e, dict)]
    rows = []
    for e in entries:
        now, prev = e.get("ranked"), e.get("previous_rank")
        move = ""
        if isinstance(now, int) and isinstance(prev, int) and prev:
            move = f"▲{prev - now}" if prev > now else f"▼{now - prev}" if now > prev else "="
        rows.append([now, move, e.get("display_name"), e.get("nationality"),
                     _num(e.get("total_flowers")), e.get("id")])
    rows, more = _limited(rows, args.limit, 20)
    out = _table(["#", "Move", "Name", "Nationality", "Flowers", "ID"], rows, flex=2)
    out += ["", f"{args.period} leaderboard · profile: mdlaw people <ID>"]
    return out + ([more] if more else [])


def _render_profile(data: object, args) -> list[str]:
    if not isinstance(data, dict):
        return _render_generic(data, args)
    name = _clean(_pick(data, "display_name", "name", "username")) or "profile"
    user = _clean(data.get("username"))
    fields = [
        ("Username", user if user != name else None),
        ("Location", data.get("location")),
        ("Gender", data.get("gender")),
        ("Joined", str(data.get("joined_at") or "")[:10]),
        ("Followers", _num(data.get("followers"))),
        ("Following", _num(data.get("following"))),
        ("Points", _num(data.get("points"))),
        ("Contributions", _num(data.get("contributions_points"))),
        ("Roles", [r.get("name") for r in data.get("roles") or [] if isinstance(r, dict)]),
        ("VIP", "yes" if data.get("vip") else None),
    ]
    if not any(_clean(v) for _, v in fields):
        return _render_generic(data, args)
    return _card(name, fields, body=data.get("about"))


# list_id → status, verified against the /sync/mylist/<status> endpoints.
_LIST_STATUS = {1: "watching", 2: "completed", 3: "plan to watch", 4: "on hold",
                5: "dropped", 6: "not interested"}


def _render_watchlist(data: object, args) -> list[str]:
    items = [i for i in _items(data, "list", "watchlist") if isinstance(i, dict)]
    if not items:
        return [f"Nothing in your {args.status or 'watching'} list."]
    rows = []
    for i in items:
        t = i.get("title") if isinstance(i.get("title"), dict) else i
        seen, total = i.get("episode_seen"), t.get("episodes")
        progress = f"{seen}/{total}" if isinstance(seen, int) and total else _clean(seen)
        score = i.get("rating")
        rows.append([t.get("id"), _pick(t, "title", "name"), _year(t),
                     _LIST_STATUS.get(i.get("list_id"), _clean(_pick(i, "status"))),
                     progress, f"{score:g}" if isinstance(score, (int, float)) and score else ""])
    show_status = len({r[3] for r in rows}) > 1
    headers = ["ID", "Title", "Year", "Status", "Progress", "Score"]
    if not show_status:  # one list → the status column is redundant
        headers.pop(3)
        rows = [r[:3] + r[4:] for r in rows]
    rows, more = _limited(rows, args.limit, 50)
    label = args.status or "watching"
    out = _table(headers, rows)
    out += ["", f"{len(items)} titles in your {label} list · details: mdlaw title <ID>"]
    if not args.status:
        out.append("other lists: mdlaw watchlist completed|plantowatch|onhold|dropped|notinterested")
    return out + ([more] if more else [])


_GENERIC_COLS = (("ID", ("id", "rid")), ("Name", ("title", "name", "display_name", "username")),
                 ("Year", ("year",)), ("Type", ("media_type", "type")),
                 ("Country", ("country", "nationality")), ("Status", ("status",)),
                 ("Rating", ("rating", "score")))


def _render_generic(data: object, args) -> list[str]:
    """Fallback for shapes without a dedicated renderer: a table for lists of
    objects, label/value rows for an object (skipping URLs and nested data)."""
    items = _items(data)
    if items and all(isinstance(i, dict) for i in items):
        cols = [(h, ks) for h, ks in _GENERIC_COLS if any(_pick(i, *ks) for i in items)]
        if cols:
            rows = [[_pick(i, *ks) for _, ks in cols] for i in items]
            rows, more = _limited(rows, getattr(args, "limit", None), None)
            flex = next((n for n, (h, _) in enumerate(cols) if h == "Name"), 0)
            return _table([h for h, _ in cols], rows, flex=flex) + ([more] if more else [])
    if isinstance(data, dict):
        fields = [(k.replace("_", " ").capitalize(), v) for k, v in data.items()
                  if not isinstance(v, (dict, list)) and not str(v).startswith("http")]
        return _card("", fields)[2:] or ["(no displayable fields — try --json)"]
    if isinstance(data, list):
        return [_clean(x) for x in data] or ["No results."]
    return [_clean(data)]


_RENDERERS = {
    "genres": _render_genres, "languages": _render_languages,
    "calendar": _render_calendar, "search": _render_search,
    "search-people": _render_search_people, "title": _render_title,
    "people": _render_people, "watchlist": _render_watchlist,
    "browse": _render_browse, "lists": _render_lists,
    "recommendations": _render_recommendations, "recs": _render_recommendations,
    "me": _render_profile, "leaderboard": _render_leaderboard,
}


# -- errors -----------------------------------------------------------------
def _explain(e: HTTPException) -> tuple[str, str | None]:
    """HTTPException → (one-line message, hint with the next step)."""
    detail = e.detail
    msg = detail.get("detail", detail) if isinstance(detail, dict) else detail
    if isinstance(msg, dict):
        msg = msg.get("message") or msg.get("error_description") or msg.get("error") or \
            json.dumps(msg, ensure_ascii=False)
    msg = " ".join(str(msg).split())
    code = e.status_code
    if code == 400 and "MDL_USERNAME" in msg and "not set" not in msg:
        return ("this command needs a MyDramaList account",
                "log in once with `mdlaw auth` (or set MDL_USERNAME and MDL_PASSWORD)")
    if code == 400 and "not set" in msg:
        return ("no MyDramaList account configured",
                "log in with `mdlaw auth`, or set MDL_USERNAME and MDL_PASSWORD")
    if code == 401:
        return (msg.split(" — ")[0] if "mdlaw auth" in msg else f"not authorized: {msg}",
                "log in again with `mdlaw auth`")
    if code == 403 and "Cloudflare" in msg:
        return ("Cloudflare blocked the request (your IP looks like a bot to it)",
                "retry with `--transport curl_cffi`, or from another network")
    if code == 404:
        return "not found on MyDramaList (HTTP 404)", "check the id — ids come from search results"
    if code == 428:
        return "this account has two-factor authentication enabled", \
            "mdlaw can't complete 2FA logins; disable 2FA or use an app password"
    if code == 429:
        return "MyDramaList is rate-limiting requests (HTTP 429)", "wait a minute and try again"
    if code == 504:
        return "MyDramaList did not respond in time", "try again; -v shows the request timings"
    if code == 502:
        return f"could not reach MyDramaList: {msg}", "check your connection and try again"
    if code >= 500:
        return f"MyDramaList returned an error (HTTP {code})", "try again later"
    return f"{msg} (HTTP {code})", None


def _print_error(message: str, hint: str | None = None) -> None:
    print(f"error: {message}", file=sys.stderr)
    if hint:
        print(f"hint:  {hint}", file=sys.stderr)


# -- logging ----------------------------------------------------------------
class _StderrHandler(logging.StreamHandler):
    """Writes to the *current* sys.stderr (survives stream swaps in tests)."""

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


class _CLIFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"warning: {msg}"
        return f"  · {msg}"


def _setup_cli_logging(verbosity: int) -> None:
    h = _StderrHandler()
    h.setFormatter(_CLIFormatter())
    log.handlers[:] = [h]
    log.setLevel(logging.WARNING if verbosity <= 0 else
                 logging.INFO if verbosity == 1 else logging.DEBUG)
    log.propagate = False


class _Spinner:
    """'Contacting MyDramaList…' on stderr when a call takes a moment. Only on
    an interactive terminal and when -v isn't printing its own progress."""

    def __init__(self, enabled: bool, text: str = "Contacting MyDramaList") -> None:
        self.enabled, self.text = enabled, text
        self._task: asyncio.Task | None = None
        self._shown = False

    async def _run(self) -> None:
        await asyncio.sleep(0.6)
        utf = (getattr(sys.stderr, "encoding", "") or "").lower().replace("-", "") == "utf8"
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if utf else "|/-\\"
        i = 0
        while True:
            self._shown = True
            sys.stderr.write(f"\r{frames[i % len(frames)]} {self.text}…")
            sys.stderr.flush()
            i += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> "_Spinner":
        if self.enabled:
            self._task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            if self._shown:
                sys.stderr.write("\r" + " " * (_dw(self.text) + 4) + "\r")
                sys.stderr.flush()


# -- commands ---------------------------------------------------------------
async def _call(args) -> object:
    mdl = MDL()
    try:
        c = args.command
        if c == "genres":
            return await mdl.genres()
        if c == "languages":
            return await mdl.languages()
        if c == "calendar":
            return await mdl.calendar()
        if c == "search":
            return await mdl.search(" ".join(args.query), country=args.country,
                                    type=args.type, year=args.year, page=args.page)
        if c == "search-people":
            return await mdl.search_people(" ".join(args.query), page=args.page)
        if c == "browse":
            return await mdl.browse(types=args.types, genres=args.genres,
                                    countries=args.countries, released_from=args.released_from,
                                    sort=args.sort, page=args.page)
        if c in ("recommendations", "recs"):
            return await mdl.recommendations(page=args.page)
        if c == "lists":
            n = max(args.limit or 10, 1)
            return await {"trending": mdl.lists_trending, "recent": mdl.lists_recent,
                          "popular": mdl.lists_popular, "featured": mdl.lists_featured}[args.which](n)
        if c == "title":
            return await mdl.title(args.id)
        if c == "people":
            return await mdl.people(args.id)
        if c == "watchlist":
            return await mdl.watchlist(args.status)
        if c == "me":
            return await mdl.me()
        if c == "leaderboard":
            return await mdl.leaderboard(args.period)
        raise _CLIError(f"unknown command: {c}", "see `mdlaw --help`", code=2)
    finally:
        await mdl.close()


def _want_json(args) -> bool:
    if args.output == "json":
        return True
    if args.output == "text":
        return False
    return not sys.stdout.isatty()


def _emit(data: object, args) -> None:
    if _want_json(args):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    render = _RENDERERS.get(args.command, _render_generic)
    print("\n".join(render(data, args)))


def _run_cli(argv: list[str]) -> int:
    """Parse arguments, run one command, print the result. Returns the exit code."""
    global TRANSPORT
    _ensure_utf8_stdio()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse: --help/--version (0) or bad usage (2)
        return e.code if isinstance(e.code, int) else 2
    if args.command is None:
        parser.print_help()
        return 0
    args.limit = getattr(args, "limit", None)
    if getattr(args, "transport", None):
        TRANSPORT = args.transport
        os.environ["MDL_TRANSPORT"] = args.transport  # serve re-imports the module
    if args.command in ("serve", "run"):
        # The server sets up its own log format (MDL_LOG_LEVEL, default INFO);
        # -v here means DEBUG.
        if args.verbose:
            os.environ["MDL_LOG_LEVEL"] = "DEBUG"
        extra = [x for k in ("host", "port") if getattr(args, k)
                 for x in (f"--{k}", getattr(args, k))]
        return _serve(extra)
    _setup_cli_logging(args.verbose)
    try:
        if args.command == "self":
            return self_check()
        if args.command == "auth":
            if args.username == "status" and not args.password:
                return _cli_auth_status(args)
            return _cli_auth(args)
        if args.command == "logout":
            return _cli_logout()
        if not auth_enabled():
            _load_auth()
        spin = sys.stderr.isatty() and args.verbose == 0 and not _want_json(args)

        async def run() -> object:
            async with _Spinner(spin):
                return await _call(args)
        _emit(asyncio.run(run()), args)
        return 0
    except HTTPException as e:
        _print_error(*_explain(e))
        return 1
    except _CLIError as e:
        _print_error(e.message, e.hint)
        return e.code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:  # unexpected: short message, traceback only with -vv
        if args.verbose >= 2:
            import traceback
            traceback.print_exc()
        _print_error(f"unexpected {type(e).__name__}: {e}",
                     "run again with -vv for the full traceback")
        return 1


def _cli_auth(args) -> int:
    """mdlaw auth [username] [password] — log in and save the session."""
    global MDL_USERNAME, MDL_PASSWORD, _PERSIST_AUTH
    username = args.username or os.environ.get("MDL_USERNAME", "").strip() or None
    password = args.password
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    password = password or os.environ.get("MDL_PASSWORD", "").strip() or None
    interactive = sys.stdin.isatty()
    if not username:
        if not interactive:
            raise _CLIError("no username given", "usage: mdlaw auth <username>", code=2)
        username = input("MDL username or email: ").strip()
    if not username:
        raise _CLIError("a username is required", code=2)
    if not password:
        if not interactive:
            raise _CLIError("no password given",
                            "pipe it with --password-stdin or set MDL_PASSWORD", code=2)
        import getpass
        password = getpass.getpass("Password: ")
    MDL_USERNAME, MDL_PASSWORD, _PERSIST_AUTH = username, password, True

    async def do_login() -> None:
        try:
            async with _Spinner(sys.stderr.isatty() and args.verbose == 0, "Logging in"):
                await login(force=True)
        finally:
            await close_client()
    asyncio.run(do_login())
    left = _fmt_duration(_auth["expires_at"] - time.time())
    if _want_json(args):
        print(json.dumps(_session_info(), indent=2, ensure_ascii=False))
    else:
        print(f"Logged in as {username}.")
        print(f"Session saved to {_AUTH_FILE} (token valid for {left}, refreshed automatically).")
    return 0


def _session_info() -> dict:
    """Saved-session summary for `auth status` — never includes tokens."""
    user = _auth["user"] if isinstance(_auth["user"], dict) else {}
    now = time.time()
    return {
        "logged_in": bool(_auth["token"] or _auth["refresh_token"]),
        "account": user.get("name") or user.get("username") or _auth.get("username") or None,
        "expires_at": _auth["expires_at"] or None,
        "expired": bool(_auth["token"]) and _auth["expires_at"] <= now,
        "can_refresh": bool(_auth["refresh_token"]),
        "session_file": _AUTH_FILE,
        "env_credentials": bool(os.environ.get("MDL_USERNAME") and os.environ.get("MDL_PASSWORD")),
    }


def _cli_auth_status(args) -> int:
    """Show the saved session: account, token state, what happens next."""
    _load_auth()
    info = _session_info()
    if _want_json(args):
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0
    env = "MDL_USERNAME / MDL_PASSWORD are set (used when the session can't be refreshed)" \
        if info["env_credentials"] else None
    if not info["logged_in"]:
        print("Not logged in.")
        if env:
            print(env)
        print("Log in with: mdlaw auth <username>")
        return 0
    left = _auth["expires_at"] - time.time()
    if not info["expired"]:
        token = f"valid for {_fmt_duration(left)}"
    elif info["can_refresh"]:
        token = f"expired {_fmt_duration(left)} ago — the next request will try to refresh it"
    else:
        token = f"expired {_fmt_duration(left)} ago — log in again with `mdlaw auth`"
    print("\n".join(_card("Saved session", [
        ("Account", info["account"] or "(unknown — log in again to record it)"),
        ("Token", token),
        ("Device id", _auth["device_id"]),
        ("File", info["session_file"]),
        ("Env", env),
    ])))
    return 0


def _cli_logout() -> int:
    """Remove the saved session."""
    try:
        os.remove(_AUTH_FILE)
        print(f"Logged out (removed {_AUTH_FILE}).")
    except FileNotFoundError:
        print("Not logged in — no saved session to remove.")
    except OSError as e:
        raise _CLIError(f"could not remove {_AUTH_FILE}: {e}") from None
    return 0


def _parse_serve_args(args: list[str]) -> tuple[str, int]:
    """--host/--port (also --host=X); defaults from HOST/PORT env, else
    0.0.0.0:8000. Raises ValueError on bad input."""
    host = os.environ.get("HOST", "").strip() or "0.0.0.0"
    port_s = os.environ.get("PORT", "").strip() or "8000"
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if name not in ("--host", "--port"):
            raise ValueError(f"unknown serve option: {a}")
        if not eq:
            if i + 1 >= len(args):
                raise ValueError(f"{name} requires a value")
            i += 1
            val = args[i]
        if name == "--host":
            host = val
        else:
            port_s = val
        i += 1
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"invalid port: {port_s}") from None
    if not 0 < port < 65536:
        raise ValueError(f"invalid port: {port}")
    return host, port


def _serve(args: list[str]) -> int:
    try:
        host, port = _parse_serve_args(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    import uvicorn
    uvicorn.run("mdlaw:app", host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (console script `mdlaw`). No command → start the server
    (as do bare --host/--port, kept for compatibility)."""
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0].partition("=")[0] in ("--host", "--port"):
        return _serve(args)
    return _run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
