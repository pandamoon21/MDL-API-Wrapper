"""Offline self-checks for mdlaw (no network). Run: python -m pytest tests/"""
import asyncio
import os

import mdlaw


def test_key_required():
    # public build: key must come from env (no embedded key)
    assert mdlaw.API_KEY == os.environ.get("MDL_API_KEY", "").strip()
    assert mdlaw.API_KEY, "MDL_API_KEY must be set in env for tests"


def test_import_without_key_ok():
    # pip install mdlaw → `import mdlaw` must work even without MDL_API_KEY;
    # the server refuses to START without it (lifespan), not the import.
    import subprocess
    code = "import mdlaw; assert not mdlaw.API_KEY; print('import-ok')"
    r = subprocess.run(["python3", "-c", code], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "MDL_API_KEY"})
    assert r.returncode == 0, r.stderr
    assert "import-ok" in r.stdout


def test_key_not_literal_in_source():
    src = open("mdlaw.py", encoding="utf-8").read()
    # public build: key must come ONLY from env — no embedded literal,
    # no fallback, no blob/XOR key machinery (b64decode is fine: it's used
    # for JWT exp decoding in auth, not for the key)
    assert 'os.environ.get("MDL_API_KEY"' in src, "key must come from env"
    assert 'or "' not in src.split("MDL_API_KEY")[1][:200], "no fallback literal"
    assert "_BLOB" not in src and "_MASK" not in src
    assert "mdlaw-xor" not in src


def test_md5_formula():
    assert mdlaw._md5("secret") == "5ebe2294ecd0e0f08eab7690d2a6ee69"


def test_auth_offline(monkeypatch):
    # auth reads env at import time — force it off so this test is
    # deterministic regardless of the surrounding shell environment
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "")
    assert mdlaw.auth_enabled() is False
    h = mdlaw.default_headers()
    assert "Authorization" not in h


def test_headers_scheme():
    h = mdlaw.default_headers()
    assert h["mdl-api-key"] == mdlaw.API_KEY
    assert h["User-Agent"] == "okhttp/4.12.0"
    assert h["Accept-Language"] == "en" and h["Accept"] == "*/*"
    assert "Authorization" not in h
    assert mdlaw.default_headers("tok")["Authorization"] == "Bearer tok"


def test_ttl_cache():
    c = mdlaw.TTLCache()
    c.put("k", {"a": 1}, 100)
    assert c.get("k") == {"a": 1}
    c.put("e", 1, -1)
    assert c.get("e") is None


def test_route_count():
    routes = [r.path for r in mdlaw.app.routes if r.path.startswith("/api/")]
    # 25 data routes + dashboard + status
    assert len(routes) == 27, routes


def test_leaderboard_validation():
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException):
        asyncio.run(mdlaw.leaderboard(period="bogus"))


def test_sql_cache_change_detection():
    s = mdlaw.SQLCache("sqlite:///:memory:")
    s.put("k", {"a": 1}, 100)
    assert s.get("k") == {"a": 1}
    s.put("k", {"a": 2}, 100)  # changed
    cur = s._cursor()
    cur.execute("SELECT changed FROM mdlaw_cache WHERE key='k'")
    row = cur.fetchone()
    assert row is not None and row[0] == 1
    cur.close()
    s.put("k", {"a": 2}, 100)  # same
    cur = s._cursor()
    cur.execute("SELECT changed FROM mdlaw_cache WHERE key='k'")
    row = cur.fetchone()
    assert row is not None and row[0] == 0
    cur.close()
