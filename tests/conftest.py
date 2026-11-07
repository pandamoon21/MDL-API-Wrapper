"""Shared test fixtures (offline)."""
import pytest

import mdlaw


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep every test away from the real ~/.mdlaw_auth.json and from state
    left behind by other tests (auth, cache). Locks, clients and in-flight
    calls are per event loop, so each asyncio.run() starts clean anyway."""
    monkeypatch.setattr(mdlaw, "_AUTH_FILE", str(tmp_path / ".mdlaw_auth.json"))
    monkeypatch.setattr(mdlaw, "_PERSIST_AUTH", False)
    monkeypatch.setattr(mdlaw, "MDL_USERNAME", "")
    monkeypatch.setattr(mdlaw, "MDL_PASSWORD", "")
    monkeypatch.setattr(mdlaw, "MIN_INTERVAL", 0.0)

    async def _no_wait(seconds):  # retry backoff: never really sleep in tests
        pass
    monkeypatch.setattr(mdlaw, "_retry_sleep", _no_wait)
    monkeypatch.setattr(mdlaw, "cache", mdlaw.TTLCache())
    # logger: the CLI/server attach handlers; start every test unconfigured
    monkeypatch.setattr(mdlaw.log, "handlers", [])
    monkeypatch.setattr(mdlaw.log, "propagate", True)
    monkeypatch.setattr(mdlaw.log, "level", 0)
    saved = dict(mdlaw._auth)
    mdlaw._auth.update(token=None, refresh_token=None, expires_at=0.0, user=None,
                       username=None, login_error=None)
    yield
    mdlaw._auth.clear()
    mdlaw._auth.update(saved)
