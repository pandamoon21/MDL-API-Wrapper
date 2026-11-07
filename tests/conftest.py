"""Shared test fixtures (offline)."""
import pytest

import mdlaw


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep every test away from the real ~/.mdlaw_auth.json and from state
    left behind by other tests (auth, cache). Locks, clients and in-flight
    calls are per event loop, so each asyncio.run() starts clean anyway."""
    monkeypatch.setattr(mdlaw.client, "_AUTH_FILE", str(tmp_path / ".mdlaw_auth.json"))
    monkeypatch.setattr(mdlaw.client, "_PERSIST_AUTH", False)
    monkeypatch.setattr(mdlaw.client, "MDL_USERNAME", "")
    monkeypatch.setattr(mdlaw.client, "MDL_PASSWORD", "")
    monkeypatch.setattr(mdlaw.client, "MIN_INTERVAL", 0.0)

    async def _no_wait(seconds):  # retry backoff: never really sleep in tests
        pass
    monkeypatch.setattr(mdlaw.client, "_retry_sleep", _no_wait)
    monkeypatch.setattr(mdlaw.caching, "cache", mdlaw.TTLCache())
    # logger: the CLI/server attach handlers; start every test unconfigured
    monkeypatch.setattr(mdlaw.log, "handlers", [])
    monkeypatch.setattr(mdlaw.log, "propagate", True)
    monkeypatch.setattr(mdlaw.log, "level", 0)
    saved = dict(mdlaw.client._auth)
    mdlaw.client._auth.update(token=None, refresh_token=None, expires_at=0.0, user=None,
                       username=None, login_error=None)
    yield
    mdlaw.client._auth.clear()
    mdlaw.client._auth.update(saved)
