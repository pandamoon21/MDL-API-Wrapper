"""SQLCache against real MySQL / PostgreSQL servers (opt-in).

Skipped unless a server URL is given, so plain ``pytest`` stays offline:

    MDL_TEST_MYSQL_URL=mysql://root:pw@127.0.0.1:3306/mdlaw_test
    MDL_TEST_PG_URL=postgresql://postgres:pw@127.0.0.1:5432/mdlaw_test

Needs ``pymysql`` / ``psycopg[binary]``. Each test drops ``mdlaw_cache`` first,
so point these at a throwaway database.
"""
import os
import time

import pytest

import mdlaw

BACKENDS = [
    pytest.param(("MDL_TEST_MYSQL_URL", "mysql"), id="mysql"),
    pytest.param(("MDL_TEST_PG_URL", "postgres"), id="postgres"),
]


@pytest.fixture(params=BACKENDS)
def sc(request):
    env, flavor = request.param
    url = os.environ.get(env, "").strip()
    if not url:
        pytest.skip(f"{env} not set")
    pytest.importorskip("pymysql" if flavor == "mysql" else "psycopg")
    cache = mdlaw.SQLCache(url)
    assert cache.label == flavor
    cur = cache._cursor()
    cur.execute("DROP TABLE IF EXISTS mdlaw_cache")
    cache._conn.commit()
    cur.close()
    cache._init_schema()
    yield cache
    cache._conn.close()


def _row(sc, key, cols="changed"):
    cur = sc._cursor()
    cur.execute(f"SELECT {cols} FROM mdlaw_cache WHERE {sc._k} = {sc._ph}", (key,))
    row = cur.fetchone()
    cur.close()
    return row


def test_set_get_roundtrip(sc):
    value = {"title": "도깨비", "n": [1, 2.5, None], "nested": {"ok": True}}
    sc.put("k", value, 60)
    assert sc.get("k") == value
    assert sc.get("missing") is None
    assert sc.stats() == {"backend": sc.label, "hits": 1, "misses": 1,
                          "hit_rate": 0.5, "entries": 1}


def test_long_key_and_large_value(sc):
    key = "GET /search?q=" + "x" * 700        # near the MySQL VARCHAR(768) key limit
    value = {"blob": "y" * 200_000}           # beyond TEXT's 64 KB on MySQL
    sc.put(key, value, 60)
    assert sc.get(key) == value


def test_expiry_is_precise(sc):
    sc.put("short", 1, 2)
    assert sc.get("short") == 1
    time.sleep(2.5)
    assert sc.get("short") is None             # expired → miss, row kept
    assert sc.entries() == 1
    # timestamps survive with sub-second precision (float4 on Postgres
    # rounded them to 128 s steps)
    expires_at, created_at = _row(sc, "short", "expires_at, created_at")
    assert abs((expires_at - created_at) - 2) < 0.01


def test_change_detection(sc):
    sc.put("k", {"v": 1}, -1)
    sc.put("k", {"v": 2}, 60)
    assert _row(sc, "k")[0] == 1
    sc.put("k", {"v": 2}, 60)
    assert _row(sc, "k")[0] == 0


def test_prune_evicts_long_expired_rows(sc):
    sc.put("live", 3, 60)
    sc.put("old", 1, -(sc.PRUNE_AFTER + 10))
    sc.put("recent", 2, -10)
    assert sc.prune() == 1
    assert sc.entries() == 2
    assert sc.get("old") is None and sc.get("live") == 3


def test_sees_writes_from_another_connection(sc):
    """Two workers sharing one database: a get() must not be pinned to the
    snapshot of a transaction its first SELECT opened."""
    other = mdlaw.SQLCache(sc.url)
    try:
        assert sc.get("shared") is None
        other.put("shared", {"from": "other"}, 60)
        assert sc.get("shared") == {"from": "other"}
    finally:
        other._conn.close()


def test_reconnects_after_connection_loss(sc):
    sc.put("k", 1, 60)
    sc._conn.close()
    assert sc.get("k") == 1
