"""Cache backends: in-memory TTL/LRU (default) or SQL (sqlite/mysql/postgres)
with response change detection. ``cache`` is the process-wide backend."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict

from ._config import _env_int, log

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

    # PostgreSQL: REAL is a 4-byte float there (sqlite's REAL is 8 bytes). At
    # Unix-epoch magnitudes a float4 step is 128 s, so expiry and timestamps
    # were off by up to two minutes. DOUBLE PRECISION keeps sub-ms precision.
    _SCHEMA_PG = _SCHEMA.replace("REAL", "DOUBLE PRECISION")

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
            from urllib.parse import unquote, urlsplit
            u = urlsplit(self.url)
            # autocommit: a bare SELECT otherwise opens a REPEATABLE READ
            # transaction that never ends, so get() kept serving the snapshot
            # from its first read and never saw rows other workers wrote.
            # Userinfo is percent-encoded in a URL (p%40ss for p@ss).
            return pymysql.connect(
                host=u.hostname or "localhost", port=u.port or 3306,
                user=unquote(u.username or ""), password=unquote(u.password or ""),
                database=u.path.lstrip("/") or "", charset="utf8mb4",
                autocommit=True)
        # postgres
        try:
            import psycopg
        except ImportError:
            raise RuntimeError("PostgreSQL cache backend requires: pip install 'psycopg[binary]'")
        # autocommit: without it every get() left the session "idle in
        # transaction", and one failed statement aborted the transaction so
        # all later queries failed until a rollback that never came.
        return psycopg.connect(self.url, autocommit=True)

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute({"mysql": self._SCHEMA_MYSQL, "postgres": self._SCHEMA_PG}
                    .get(self._flavor, self._SCHEMA))
        self._conn.commit()
        cur.close()

    def _dead(self) -> bool:
        """True when the driver has marked the connection unusable. Creating a
        cursor never touches the network (pymysql) or fails only once closed
        (psycopg), so a dropped server is seen on the failing query and the
        next operation reconnects here."""
        if self._flavor == "mysql":
            return not getattr(self._conn, "open", True)
        if self._flavor == "postgres":
            return bool(getattr(self._conn, "closed", False))
        return False

    def _cursor(self):
        if self._conn is None:
            self._conn = self._connect()
            self._init_schema()
        elif self._dead():
            log.warning("%s cache connection lost; reconnecting", self._flavor)
            self._conn = self._connect()
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
