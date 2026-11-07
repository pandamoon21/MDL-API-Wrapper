"""Offline tests for bounded retry of transient upstream failures.
No network and no real waiting: upstream is a fake client and the retry
sleep is recorded instead of awaited."""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

import mdlaw


class FakeClient:
    """Returns queued responses (or raises queued exceptions) in order; the
    last item repeats. Records every request."""

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


def resp(status=200, data=None, headers=None):
    return httpx.Response(status, content=json.dumps(data if data is not None else {}).encode(),
                          headers=headers)


def run(coro, timeout=3):
    return asyncio.run(asyncio.wait_for(coro, timeout))


@pytest.fixture
def sleeps(monkeypatch):
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)
    monkeypatch.setattr(mdlaw, "_retry_sleep", fake_sleep)
    monkeypatch.setattr(mdlaw, "RETRIES", 2)
    return waited


def use(monkeypatch, client):
    monkeypatch.setattr(mdlaw, "get_client", lambda: client)
    return client


def test_success_after_transient_503(monkeypatch, sleeps):
    c = use(monkeypatch, FakeClient(resp(503), resp(data={"ok": 1})))
    assert run(mdlaw.fetch("GET", "/genres", ttl=60)) == {"ok": 1}
    assert len(c.calls) == 2 and len(sleeps) == 1


def test_transport_errors_are_retried(monkeypatch, sleeps):
    c = use(monkeypatch, FakeClient(httpx.ConnectError("boom"), httpx.ReadTimeout("slow"),
                                    resp(data={"ok": 1})))
    assert run(mdlaw.fetch("GET", "/genres", ttl=60)) == {"ok": 1}
    assert len(c.calls) == 3 and len(sleeps) == 2


def test_gives_up_after_n_retries(monkeypatch, sleeps):
    monkeypatch.setattr(mdlaw, "RETRIES", 3)
    c = use(monkeypatch, FakeClient(resp(502)))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 502
    assert len(c.calls) == 4 and len(sleeps) == 3
    # exponential backoff with jitter: each delay within [base/2, base]
    for i, d in enumerate(sleeps, start=1):
        base = mdlaw.RETRY_BASE_DELAY * 2 ** (i - 1)
        assert base / 2 <= d <= base


def test_gives_up_on_repeated_timeout(monkeypatch, sleeps):
    c = use(monkeypatch, FakeClient(httpx.ReadTimeout("slow")))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 504 and len(c.calls) == 3


def test_retry_after_respected(monkeypatch, sleeps):
    c = use(monkeypatch, FakeClient(resp(429, headers={"Retry-After": "3"}),
                                    resp(data={"ok": 1})))
    assert run(mdlaw.fetch("GET", "/genres", ttl=60)) == {"ok": 1}
    assert sleeps == [3.0] and len(c.calls) == 2


def test_retry_after_beyond_cap_is_not_waited(monkeypatch, sleeps):
    c = use(monkeypatch, FakeClient(resp(503, headers={"Retry-After": "120"}),
                                    resp(data={"ok": 1})))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 503
    assert sleeps == [] and len(c.calls) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_no_retry_on_other_errors(monkeypatch, sleeps, status):
    c = use(monkeypatch, FakeClient(resp(status), resp(data={"ok": 1})))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == status
    assert len(c.calls) == 1 and sleeps == []


def test_read_only_post_search_is_retried(monkeypatch, sleeps):
    c = use(monkeypatch, FakeClient(resp(503), resp(data={"ok": 1})))
    path = mdlaw._qs("/search/titles", edge=1, q="x")
    assert run(mdlaw.fetch("POST", path, ttl=60)) == {"ok": 1}
    assert len(c.calls) == 2


def test_no_retry_for_non_idempotent_post(monkeypatch, sleeps):
    c = use(monkeypatch, FakeClient(resp(503), resp(data={"ok": 1})))
    with pytest.raises(HTTPException):
        run(mdlaw.fetch("POST", "/users/me/watchlist", ttl=60, body={"id": 1}))
    assert len(c.calls) == 1 and sleeps == []


def test_auth_calls_are_never_retried(monkeypatch, sleeps):
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "u")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "p")
    c = use(monkeypatch, FakeClient(resp(503), resp(data={"access_token": "t"})))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.login(force=True))
    assert ei.value.status_code == 503
    assert len(c.calls) == 1 and sleeps == []


def test_retries_zero_disables(monkeypatch, sleeps):
    monkeypatch.setattr(mdlaw, "RETRIES", 0)
    c = use(monkeypatch, FakeClient(resp(503), resp(data={"ok": 1})))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/genres", ttl=60))
    assert ei.value.status_code == 503
    assert len(c.calls) == 1 and sleeps == []


def test_401_refresh_and_retries_share_one_bound(monkeypatch, sleeps):
    """A 401 refreshes once; transient retries don't reset after it."""
    mdlaw._auth.update(token="old", refresh_token="r", expires_at=9e9)
    c = use(monkeypatch, FakeClient(
        resp(503),                                        # retry 1
        resp(401),                                        # -> refresh
        resp(data={"access_token": "new", "expires_in": 3600}),  # /auth/refresh
        resp(503),                                        # retry 2
        resp(401),                                        # no second refresh
        resp(data={"ok": 1})))
    with pytest.raises(HTTPException) as ei:
        run(mdlaw.fetch("GET", "/users/me", ttl=60, auth=True))
    assert ei.value.status_code == 401
    urls = [u for _, u, _, _ in c.calls]
    assert sum("/auth/refresh" in u for u in urls) == 1
    assert sum("/users/me" in u for u in urls) == 4   # 1 + 2 retries + 1 refresh retry
    assert len(sleeps) == 2


def test_401_behaviour_unchanged(monkeypatch, sleeps):
    mdlaw._auth.update(token="old", refresh_token="r", expires_at=9e9)
    c = use(monkeypatch, FakeClient(
        resp(401),
        resp(data={"access_token": "new", "expires_in": 3600}),
        resp(data={"me": 1})))
    assert run(mdlaw.fetch("GET", "/users/me", ttl=60, auth=True)) == {"me": 1}
    assert c.calls[-1][2]["Authorization"] == "Bearer new"
    assert sleeps == []


def test_retry_after_http_date():
    from email.utils import formatdate
    import time
    r = resp(429, headers={"Retry-After": formatdate(time.time() + 5, usegmt=True)})
    assert 3 <= mdlaw._retry_after(r) <= 5
    assert mdlaw._retry_after(resp(429, headers={"Retry-After": "soon"})) is None
    assert mdlaw._retry_after(resp(429)) is None
