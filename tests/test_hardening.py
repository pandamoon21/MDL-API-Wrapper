"""Offline regression tests for the auth / error-handling / caching fixes.
No network: upstream is replaced by a fake client."""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import mdlaw


class FakeClient:
    """Stands in for the httpx / curl_cffi client: returns queued responses
    (or raises queued exceptions) and records every request."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    async def request(self, method, url, headers=None, json=None):
        self.calls.append((method, url, headers, json))
        await asyncio.sleep(0)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, BaseException):
            raise item
        return item


def resp(status=200, data=None, content=None):
    if content is None:
        content = json.dumps(data if data is not None else {}).encode()
    return httpx.Response(status, content=content)


def use(monkeypatch, client):
    monkeypatch.setattr(mdlaw, "get_client", lambda: client)
    return client


def run(coro, timeout=3):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# --- deadlock -------------------------------------------------------------

def test_refresh_without_refresh_token_does_not_deadlock(monkeypatch):
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "u")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "p")
    c = use(monkeypatch, FakeClient(resp(data={"access_token": "new", "expires_in": 3600})))
    out = run(mdlaw.refresh())  # used to hang forever on the re-entered lock
    assert out["token"] == "new"
    assert "/auth/login" in c.calls[0][1]


def test_failed_refresh_falls_back_to_login(monkeypatch):
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "u")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "p")
    mdlaw._auth.update(token="old", refresh_token="r")
    c = use(monkeypatch, FakeClient(
        resp(400, {"error": "invalid_grant"}),
        resp(data={"access_token": "fresh", "expires_in": 3600})))
    out = run(mdlaw.refresh())
    assert out["token"] == "fresh"
    assert ["/auth/refresh" in c.calls[0][1], "/auth/login" in c.calls[1][1]] == [True, True]


def test_concurrent_refresh_runs_once(monkeypatch):
    mdlaw._auth.update(token="old", refresh_token="r", expires_at=0.0)
    c = use(monkeypatch, FakeClient(resp(data={"access_token": "new", "expires_in": 3600})))

    async def burst():
        await asyncio.gather(*(mdlaw.refresh(stale_token="old") for _ in range(5)))
    run(burst())
    assert len(c.calls) == 1
    assert mdlaw._auth["token"] == "new" and mdlaw._auth["refresh_token"] == "r"


# --- transport errors (curl_cffi is the default transport) ----------------

def test_curl_cffi_timeout_maps_to_504(monkeypatch):
    from curl_cffi.requests import exceptions as ce
    use(monkeypatch, FakeClient(ce.Timeout("timed out")))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 504


def test_curl_cffi_connection_error_maps_to_502(monkeypatch):
    from curl_cffi.requests import exceptions as ce
    use(monkeypatch, FakeClient(ce.ConnectionError("reset")))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 502


def test_cloudflare_403_message_reaches_caller(monkeypatch):
    html = b"<!DOCTYPE html><title>Just a moment...</title>"
    use(monkeypatch, FakeClient(resp(403, content=html)))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 403 and "Cloudflare" in str(ei.value.detail)


def test_upstream_status_is_propagated_and_not_cached(monkeypatch):
    use(monkeypatch, FakeClient(resp(404, {"message": "nope"})))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/people/1", ttl=60))
    assert ei.value.status_code == 404
    assert mdlaw.cache.entries() == 0


def test_non_json_200_is_502(monkeypatch):
    use(monkeypatch, FakeClient(resp(200, content=b"<html>maintenance</html>")))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 502


def test_non_utf8_json_is_decoded_leniently(monkeypatch):
    use(monkeypatch, FakeClient(resp(200, content=b'{"name": "caf\xe9"}')))
    assert run(mdlaw.fetch("GET", "/users/1", ttl=60))["name"].startswith("caf")


def test_auth_request_raises_on_error_status(monkeypatch):
    use(monkeypatch, FakeClient(resp(500, content=b"<html>oops</html>")))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw._auth_request("POST", "/auth/login", body={}))
    assert ei.value.status_code == 500


# --- request coalescing ---------------------------------------------------

def test_concurrent_misses_share_one_upstream_call(monkeypatch):
    c = use(monkeypatch, FakeClient(resp(data=[{"id": 1}])))

    async def burst():
        out = await asyncio.gather(*(mdlaw.fetch("GET", "/genres", ttl=60) for _ in range(4)))
        await asyncio.sleep(0)  # let the done-callback clear the in-flight entry
        return out, dict(mdlaw._state().inflight)
    results, inflight = run(burst())
    assert len(c.calls) == 1
    assert all(r == [{"id": 1}] for r in results)
    assert inflight == {}


# --- query-string encoding ------------------------------------------------

def test_qs_encodes_user_input():
    assert mdlaw._qs("/search/titles", edge=1, q="a&page=9#x", page=1) == \
        "/search/titles?edge=1&q=a%26page%3D9%23x&page=1"


def test_search_query_is_encoded(monkeypatch):
    calls = []

    async def fake_fetch(method, path, ttl=3600, auth=False, body=None):
        calls.append(path)
        return []
    monkeypatch.setattr(mdlaw, "fetch", fake_fetch)

    async def go():
        mdl = mdlaw.MDL()
        await mdl.search("love & war", page=2)
        await mdl.search_people("Lee Min-ho")
        await mdl.search_tags("slice of life")
    asyncio.run(go())
    assert calls == [
        "/search/titles?edge=1&q=love+%26+war&page=2&synopsis=1",
        "/search/people?q=Lee+Min-ho&page=1",
        "/tags/search?q=slice+of+life",
    ]


# --- saved session --------------------------------------------------------

def test_load_auth_does_not_inject_fake_credentials(tmp_path, monkeypatch):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"token": "t", "refresh_token": "r", "expires_at": 0}))
    monkeypatch.setattr(mdlaw, "_AUTH_FILE", str(f))
    assert mdlaw._load_auth() is True
    assert mdlaw.MDL_USERNAME == "" and mdlaw.MDL_PASSWORD == ""
    assert mdlaw.auth_enabled() is True


def test_expired_saved_session_gives_clear_error(monkeypatch):
    mdlaw._auth.update(token="t", refresh_token="r")
    c = use(monkeypatch, FakeClient(resp(401, {"error": "invalid_grant"})))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.refresh())
    assert ei.value.status_code == 401 and "mdlaw auth" in str(ei.value.detail)
    assert len(c.calls) == 1  # no login attempt with bogus credentials


def test_server_does_not_write_session_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "u")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "p")
    use(monkeypatch, FakeClient(resp(data={"access_token": "tok", "expires_in": 60})))
    run(mdlaw.login(force=True))
    assert not (tmp_path / ".mdlaw_auth.json").exists()


def test_session_write_failure_only_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mdlaw, "_PERSIST_AUTH", True)
    monkeypatch.setattr(mdlaw, "_AUTH_FILE", str(tmp_path / "missing-dir" / "s.json"))
    mdlaw._save_auth()  # must not raise
    assert "could not save session" in capsys.readouterr().err


def test_cli_session_file_written(tmp_path, monkeypatch):
    monkeypatch.setattr(mdlaw, "_PERSIST_AUTH", True)
    mdlaw._auth.update(token="t", refresh_token="r", username="me")
    mdlaw._save_auth()
    data = json.loads((tmp_path / ".mdlaw_auth.json").read_text())
    assert data["token"] == "t" and data["username"] == "me"


# --- HTTP layer: admin-gated auth routes, rate limit, stats ---------------

def test_auth_routes_disabled_without_admin_token(monkeypatch):
    monkeypatch.setattr(mdlaw, "MDL_ADMIN_TOKEN", "")
    with TestClient(mdlaw.app) as cl:
        assert cl.post("/api/v1/auth/login").status_code == 404
        assert cl.post("/api/v1/auth/refresh").status_code == 404


def test_auth_routes_never_return_tokens(monkeypatch):
    monkeypatch.setattr(mdlaw, "MDL_ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "u")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "p")
    use(monkeypatch, FakeClient(resp(data={"access_token": "TOKEN-XYZ",
                                           "refresh_token": "REFRESH-XYZ",
                                           "expires_in": 3600})))
    with TestClient(mdlaw.app) as cl:
        assert cl.post("/api/v1/auth/login").status_code == 403
        assert cl.post("/api/v1/auth/login",
                       headers={"X-Admin-Token": "wrong"}).status_code == 403
        r = cl.post("/api/v1/auth/login", headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 200 and r.json()["logged_in"] is True
    assert "TOKEN-XYZ" not in r.text and "REFRESH-XYZ" not in r.text


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(mdlaw, "RATE_LIMIT", 2)
    monkeypatch.setattr(mdlaw, "_rate", {})
    with TestClient(mdlaw.app) as cl:
        codes = [cl.get("/api/v1/cache/stats").status_code for _ in range(3)]
        health = [cl.get("/api/v1/health").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert health == [200, 200, 200]  # health probe is exempt


def test_rate_limit_off_by_default(monkeypatch):
    monkeypatch.setattr(mdlaw, "RATE_LIMIT", 0)
    with TestClient(mdlaw.app) as cl:
        assert all(cl.get("/api/v1/cache/stats").status_code == 200 for _ in range(5))


def test_cache_stats_shape():
    with TestClient(mdlaw.app) as cl:
        s = cl.get("/api/v1/cache/stats").json()
        d = cl.get("/api/v1/dashboard").json()["cache"]
    assert s == d
    assert set(s) == {"backend", "hits", "misses", "hit_rate", "entries"}
    assert s["backend"] == "memory"


# --- SQL cache ------------------------------------------------------------

def test_fetch_with_sqlite_cache(monkeypatch):
    monkeypatch.setattr(mdlaw, "cache", mdlaw.SQLCache("sqlite:///:memory:"))
    c = use(monkeypatch, FakeClient(resp(data={"ok": 1})))
    assert run(mdlaw.fetch("GET", "/genres", ttl=60)) == {"ok": 1}
    assert run(mdlaw.fetch("GET", "/genres", ttl=60)) == {"ok": 1}
    assert len(c.calls) == 1
    assert mdlaw.cache.stats()["backend"] == "sqlite"


def test_mysql_sql_quotes_reserved_key(monkeypatch):
    """No MySQL server offline: capture the SQL the backend would send."""
    executed = []

    class Cur:
        rowcount = 0

        def execute(self, sql, params=()):
            executed.append(sql)

        def fetchone(self):
            return None

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

    sc = mdlaw.SQLCache("mysql://u:p@localhost/db")
    monkeypatch.setattr(sc, "_connect", lambda: Conn())  # pymysql has no Connection.execute
    sc.put("k", {"a": 1}, 60)
    sc.get("k")
    schema = executed[0]
    assert "`key`" in schema and "VARCHAR(768)" in schema and "TEXT PRIMARY KEY" not in schema
    for sql in executed[1:]:
        assert " key " not in sql.replace("`key`", "") and "(key," not in sql


# --- CLI serve options ----------------------------------------------------

def test_serve_args(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert mdlaw._parse_serve_args([]) == ("0.0.0.0", 8000)
    assert mdlaw._parse_serve_args(["--port", "9000"]) == ("0.0.0.0", 9000)
    assert mdlaw._parse_serve_args(["--host=127.0.0.1", "--port=1"]) == ("127.0.0.1", 1)
    monkeypatch.setenv("PORT", "8080")
    assert mdlaw._parse_serve_args([]) == ("0.0.0.0", 8080)
    for bad in (["--port"], ["--port", "x"], ["--port", "70000"], ["--bogus"]):
        with pytest.raises(ValueError):
            mdlaw._parse_serve_args(bad)


# --- round 2: change detection, bounded cache, event loops, validation ----

def test_sql_change_detection_through_fetch(monkeypatch):
    """Real flow: entry expires, fetch() misses, upstream returns a new value
    → changed=1. get() used to delete the expired row, so this was always 0."""
    sc = mdlaw.SQLCache("sqlite:///:memory:")
    monkeypatch.setattr(mdlaw, "cache", sc)
    use(monkeypatch, FakeClient(resp(data={"v": 1})))
    run(mdlaw.fetch("GET", "/genres", ttl=-1))           # stored already expired
    use(monkeypatch, FakeClient(resp(data={"v": 2})))
    assert run(mdlaw.fetch("GET", "/genres", ttl=60)) == {"v": 2}

    def changed():
        cur = sc._cursor()
        cur.execute("SELECT changed FROM mdlaw_cache WHERE key = 'GET /genres'")
        v = cur.fetchone()[0]
        cur.close()
        return v
    assert changed() == 1
    sc.put("GET /genres", {"v": 2}, -1)                  # same value again
    assert changed() == 0


def test_sql_prune_removes_only_long_expired_rows():
    sc = mdlaw.SQLCache("sqlite:///:memory:")
    sc.put("live", 3, 60)          # first put also runs an (empty) prune
    sc.put("old", 1, -(sc.PRUNE_AFTER + 10))
    sc.put("recent", 2, -10)       # expired, but kept for change detection
    assert sc.prune() == 1
    assert sc.entries() == 2
    assert sc.get("old") is None and sc.get("live") == 3


def test_memory_cache_is_bounded_lru():
    c = mdlaw.TTLCache(max_entries=3)
    for k in "abc":
        c.put(k, k, 60)
    assert c.get("a") == "a"        # touch a → b is now least recently used
    c.put("d", "d", 60)
    assert c.entries() == 3
    assert c.get("b") is None and c.get("a") == "a" and c.get("d") == "d"


def test_memory_cache_max_from_env(monkeypatch):
    monkeypatch.setenv("MDL_CACHE_MAX_ENTRIES", "7")
    assert mdlaw.TTLCache().max_entries == 7


def test_library_survives_multiple_asyncio_runs(monkeypatch):
    """asyncio.run() twice used to fail with 'Event loop is closed' because the
    client and locks were bound to the first loop."""
    clients = []

    def factory():
        st = mdlaw._state()
        if st.httpx is None:
            st.httpx = FakeClient(resp(data={"ok": 1}))
            clients.append(st.httpx)
        return st.httpx
    monkeypatch.setattr(mdlaw, "get_client", factory)

    async def once(i):
        # contend on the locks so they actually bind to this loop
        return await asyncio.gather(*(mdlaw.fetch("GET", f"/x{i}/{j}", ttl=60)
                                      for j in range(3)))
    assert run(once(1)) == [{"ok": 1}] * 3
    assert run(once(2)) == [{"ok": 1}] * 3
    assert len(clients) == 2  # one client per loop


def test_route_param_validation(monkeypatch):
    called = []

    async def fake_fetch(*a, **k):
        called.append(a)
        return {}
    monkeypatch.setattr(mdlaw, "fetch", fake_fetch)
    with TestClient(mdlaw.app) as cl:
        bad = [
            cl.get("/api/v1/titles/trending?page=0"),
            cl.get("/api/v1/lists/featured?limit=1000"),
            cl.get("/api/v1/people/0"),
            cl.get("/api/v1/tags/search?q=" + "x" * 201),
            cl.post("/api/v1/search?q="),
        ]
        ok = [
            cl.get("/api/v1/titles/trending?page=2"),
            cl.get("/api/v1/people/12"),
            cl.post("/api/v1/search?q=crash"),
        ]
    assert [r.status_code for r in bad] == [422] * 5
    assert [r.status_code for r in ok] == [200] * 3
    assert len(called) == 3
