"""Shared test fixtures (offline)."""
import pytest

import mdlaw


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep every test away from the real ~/.mdlaw_auth.json and from state
    left behind by other tests (auth, cache, in-flight calls, asyncio locks
    bound to a previous test's event loop)."""
    import asyncio as _aio
    monkeypatch.setattr(mdlaw, "_AUTH_FILE", str(tmp_path / ".mdlaw_auth.json"))
    monkeypatch.setattr(mdlaw, "_PERSIST_AUTH", False)
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "")
    monkeypatch.setattr(mdlaw, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(mdlaw, "cache", mdlaw.TTLCache())
    monkeypatch.setattr(mdlaw, "_auth_lock", _aio.Lock())
    monkeypatch.setattr(mdlaw, "_out_lock", _aio.Lock())
    monkeypatch.setattr(mdlaw, "_SEM", _aio.Semaphore(2))
    saved = dict(mdlaw._auth)
    mdlaw._auth.update(token=None, refresh_token=None, expires_at=0.0, user=None,
                       username=None, login_error=None)
    mdlaw._inflight.clear()
    yield
    mdlaw._auth.clear()
    mdlaw._auth.update(saved)
