"""Offline checks for narrowed exception handlers, hidden payment routes and
SQLCache driver settings."""
import asyncio
import sys
import types

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import mdlaw


def test_payment_routes_hidden_from_schema_but_served(monkeypatch):
    calls = []

    async def fake_fetch(method, path, ttl=3600, auth=False, body=None, cache_key=None):
        calls.append(path)
        return {"ok": path}
    monkeypatch.setattr(mdlaw.client, "fetch", fake_fetch)
    with TestClient(mdlaw.app) as cl:
        paths = cl.get("/openapi.json").json()["paths"]
        assert not any(p.startswith("/api/v1/payment") for p in paths)
        assert "/api/v1/genres" in paths
        assert cl.get("/api/v1/payment/plans").json() == {"ok": "/payment/plans"}
        assert cl.get("/api/v1/payment/coins").json() == {"ok": "/payment/coins"}
    assert calls == ["/payment/plans", "/payment/coins"]


def _browse_by_genre(monkeypatch, title_error):
    async def fake_get(self, path, ttl=3600, auth=False):
        return [{"id": 1}, {"id": 2}]

    async def fake_title(self, tid):
        if tid == 1:
            raise title_error
        return {"id": tid, "genres": [{"id": 7}]}
    monkeypatch.setattr(mdlaw.MDL, "get", fake_get)
    monkeypatch.setattr(mdlaw.MDL, "title", fake_title)
    return asyncio.run(mdlaw.MDL().browse_by_genre(7, limit=5, source="trending"))


def test_browse_by_genre_skips_per_title_failures(monkeypatch):
    assert _browse_by_genre(monkeypatch, HTTPException(404)) == [{"id": 2, "genres": [{"id": 7}]}]


@pytest.mark.parametrize("status", [400, 401, 403, 428])
def test_browse_by_genre_surfaces_global_failures(monkeypatch, status):
    with pytest.raises(HTTPException) as ei:
        _browse_by_genre(monkeypatch, HTTPException(status))
    assert ei.value.status_code == status


def test_browse_by_genre_does_not_hide_bugs(monkeypatch):
    with pytest.raises(KeyError):
        _browse_by_genre(monkeypatch, KeyError("bug"))


@pytest.mark.parametrize("token", [None, "opaque", "a.!!!.c", "a.bm90LWpzb24.c",
                                   "a.WzFd.c", "a.eyJleHAiOiJ4In0.c"])
def test_set_tokens_falls_back_on_undecodable_jwt(token):
    before = mdlaw.client.time.time()
    mdlaw.client._set_tokens({"access_token": token})
    assert mdlaw.client._auth["expires_at"] >= before + 6 * 3600 - 1


def test_upstream_error_non_json_body():
    assert mdlaw.client._upstream_error(500, "oops").detail["detail"] == "oops"
    assert mdlaw.client._upstream_error(500, '{"m": 1}').detail["detail"] == {"m": 1}


def test_sql_drivers_use_autocommit_and_decode_userinfo(monkeypatch):
    seen = {}
    monkeypatch.setitem(sys.modules, "pymysql", types.SimpleNamespace(
        connect=lambda **kw: seen.setdefault("mysql", kw)))
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(
        connect=lambda url, **kw: seen.setdefault("pg", (url, kw))))
    mdlaw.SQLCache("mysql://us%3Aer:p%40ss@db:3307/c")._connect()
    assert seen["mysql"]["autocommit"] is True
    assert (seen["mysql"]["user"], seen["mysql"]["password"]) == ("us:er", "p@ss")
    assert (seen["mysql"]["host"], seen["mysql"]["port"], seen["mysql"]["database"]) == ("db", 3307, "c")
    mdlaw.SQLCache("postgresql://u:p@db/c")._connect()
    assert seen["pg"] == ("postgresql://u:p@db/c", {"autocommit": True})


def test_postgres_schema_uses_double_precision():
    assert "REAL" not in mdlaw.SQLCache._SCHEMA_PG
    assert mdlaw.SQLCache._SCHEMA_PG.count("DOUBLE PRECISION") == 3


class _Conn:
    def __init__(self, **state):
        self.__dict__.update(state)

    def cursor(self):
        return "cur"


@pytest.mark.parametrize("url,state", [("mysql://u:p@h/d", {"open": False}),
                                       ("postgresql://u:p@h/d", {"closed": True})])
def test_dead_connection_is_replaced(monkeypatch, url, state):
    sc = mdlaw.SQLCache(url)
    sc._conn = _Conn(**state)
    fresh = _Conn()
    monkeypatch.setattr(sc, "_connect", lambda: fresh)
    assert sc._cursor() == "cur" and sc._conn is fresh
    sc._cursor()
    assert sc._conn is fresh  # a live connection is kept
