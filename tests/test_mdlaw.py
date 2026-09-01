"""Offline self-checks for mdlaw (no network). Run: python -m pytest tests/"""
import asyncio
import os

import mdlaw


def test_key_optional():
    # public build: mdl-api-key is a client nonce, not a secret. API_KEY comes
    # from MDL_API_KEY env if set, otherwise a random 20-char nonce is generated.
    assert len(mdlaw.API_KEY) == 20
    if os.environ.get("MDL_API_KEY"):
        assert mdlaw.API_KEY == os.environ["MDL_API_KEY"].strip()
    # default transport is httpx; curl_cffi is opt-in via env
    assert mdlaw.TRANSPORT in ("httpx", "curl_cffi")


def test_import_without_key_ok():
    # pip install mdlaw → `import mdlaw` works with no env at all; API_KEY is
    # generated as a nonce. Server also starts fine (no key requirement).
    import subprocess
    code = "import mdlaw; assert mdlaw.API_KEY and len(mdlaw.API_KEY) == 20; print('import-ok')"
    r = subprocess.run(["python3", "-c", code], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "MDL_API_KEY"})
    assert r.returncode == 0, r.stderr
    assert "import-ok" in r.stdout


def test_key_not_literal_in_source():
    src = open("mdlaw.py", encoding="utf-8").read()
    # public build: no embedded literal key, no fixed fallback — the fallback
    # is a runtime-generated nonce (secrets + string + range(20)). No blob/XOR
    # key machinery (b64decode is fine: used for JWT exp decoding in auth).
    assert 'os.environ.get("MDL_API_KEY"' in src, "key must come from env"
    assert "secrets.choice(string.ascii_letters + string.digits)" in src, "fallback must be a generated nonce"
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


def test_mdl_library_layer(monkeypatch):
    # MDL is a Pythonic wrapper over fetch(): verify method → path mapping
    # offline by stubbing fetch (no network).
    calls = []
    async def fake_fetch(method, path, ttl=3600, auth=False, body=None):
        calls.append((method, path, ttl, auth, body))
        return {"ok": True}
    monkeypatch.setattr(mdlaw, "fetch", fake_fetch)

    async def run():
        mdl = mdlaw.MDL()
        await mdl.genres()
        await mdl.title(686)
        await mdl.search("crash")
        await mdl.watchlist()
        await mdl.watchlist("completed")
        await mdl.me()
        try:
            await mdl.watchlist("bogus")
        except Exception:
            pass  # invalid status → HTTPException
        return mdl
    asyncio.run(run())
    expected = [
        ("GET", "/genres", 3600, False, None),
        ("GET", "/titles/686?expand=1", 300, True, None),
        ("POST", "/search", 300, True, {"q": "crash", "synopsis": 1}),
        ("GET", "/sync/mylist/watchlist", 60, True, None),
        ("GET", "/sync/mylist/completed", 60, True, None),
        ("GET", "/users/me", 60, True, None),
    ]
    assert calls == expected, calls


def test_mdl_constructor_auth(monkeypatch):
    # MDL(username=..., password=...) sets the module-level credentials used by
    # login()/auth_headers(). Verify without network.
    mdl = mdlaw.MDL(username="user@example.com", password="s3cret")
    assert mdlaw.MDL_USERNAME == "user@example.com"
    assert mdlaw.MDL_PASSWORD == "s3cret"
    assert mdlaw.auth_enabled() is True
    # reset so other tests aren't affected
    mdlaw.MDL_USERNAME = ""
    mdlaw.MDL_PASSWORD = ""
