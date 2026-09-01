# mdlaw — Agent Instructions

`mdlaw` is a single-file FastAPI wrapper over the official MyDramaList Android
app API (`app-api.mydramalist.com/v1`). One Python file (`mdlaw.py`), no build
step. This file tells an AI agent (or any new developer) how to get it running
and what to watch out for.

## Quick start (local)

```bash
cd MDL-API-Wrapper

# 1. Create venv & install
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2. Set the required API key (not in source — you provide it)
export MDL_API_KEY=<your-key>

# 3. (Optional) enable account features
export MDL_USERNAME=your_username
export MDL_PASSWORD=your_password

# 4. Run
uvicorn mdlaw:app --port 8000
# → http://localhost:8000/docs
```

That's it. No database, no build, no migrations. The default cache is
in-memory.

## Verify it works

```bash
# health + one data endpoint
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/genres

# offline self-check (no network, no server needed)
MDL_API_KEY=<your-key> python3 mdlaw.py self

# full test suite (offline)
MDL_API_KEY=<your-key> python3 -m pytest tests/ -q   # expect: 8 passed
```

## Project layout

```
mdlaw.py            # the entire API (config, client, cache, auth, routes)
tests/test_mdlaw.py # 8 offline checks (no network)
api/index.py        # Vercel serverless entrypoint
vercel.json         # Vercel config
Dockerfile          # python:3.12-slim, non-root
fly.toml            # Fly.io config
requirements.txt    # fastapi, uvicorn[standard], httpx (+ optional DB drivers)
.env.example        # all env vars documented
pytest.ini          # pythonpath = .
```

## Environment variables

| Var | Purpose | Required | Default |
|---|---|---|---|
| `MDL_API_KEY` | The MDL app API key (never in source) | **yes** | — |
| `MDL_USERNAME` + `MDL_PASSWORD` | Enable auth-gated endpoints (title detail, search, watchlist, /me) | no | disabled |
| `MDL_CACHE_BACKEND` | `memory` \| `sqlite` \| `mysql` \| `postgres` | no | `memory` |
| `MDL_CACHE_DB_URL` | DSN for SQL backends, e.g. `sqlite:///mdlaw_cache.db` | only if backend ≠ memory | — |

Copy `.env.example` to `.env` and fill in values; `mdlaw` reads env vars at
import time (no dotenv loader — set them in the shell or a process manager).

## How it works (the 30-second version)

- **Upstream**: the MDL Android app's JSON API behind Cloudflare. It 403s
  without the exact header scheme (`User-Agent: okhttp/4.12.0`, `mdl-api-key`,
  `X-Client-*`). `default_headers()` reproduces it.
- **Throttle**: outbound is limited (2 concurrent, 0.5 s min interval) — the
  WAF soft-blocks bursts. Never remove this.
- **Cache**: `TTLCache` (memory) or `SQLCache` (sqlite/mysql/postgres). Stored
  responses carry a sha256 hash; on TTL expiry the upstream is re-fetched and
  the hash is compared → `changed=1` if the response actually changed.
- **Auth**: logs in once with env credentials (`POST /auth/login`, password
  MD5-hashed), decodes JWT `exp`, refreshes before expiry, re-logs in if
  refresh fails. 2FA is NOT supported (returns 428).

## Conventions & pitfalls

- **Do not remove the throttle or the cache** — that's what keeps us under the
  WAF. A burst of uncached requests → soft 404s.
- **Don't change the header scheme** unless MDL updates the app. If endpoints
  start 403ing, the constants at the top of `mdlaw.py` are the single place to
  update.
- **Never put the real `MDL_API_KEY` / account credentials in source** — they
  live in env. This public repo must stay free of any embedded key or
  obfuscation machinery.
- **Run `python3 mdlaw.py self`** before committing — it's a fast offline
  sanity check (headers, cache, MD5 formula, SQL change detection).
- **Keep tests offline** — `tests/` must never hit the network.
- **Single-file rule** — new endpoints go in `mdlaw.py` unless there's a real
  reason to split. Avoid adding dependencies; `fastapi` + `httpx` + stdlib
  cover 99% of needs. Optional DB drivers (`pymysql`, `psycopg`) stay optional.
- **Auth-gated endpoints** need `auth=True` in the `fetch()` call; without
  credentials they return a clear 400 (handled centrally in `fetch`).

## Deploy

- **Fly.io** (recommended): `fly.toml` included → `fly launch --no-deploy`,
  `fly secrets set MDL_API_KEY=... MDL_USERNAME=... MDL_PASSWORD=...`,
  `fly deploy`.
- **Vercel**: `api/index.py` + `vercel.json` included. Note: cold starts
  (~2–5 s) and per-instance ephemeral cache — fine for demos, not for
  latency-critical use.
- **Docker**: `docker build -t mdlaw . && docker run -p 8000:8000 -e MDL_API_KEY=... mdlaw`
  (pass env vars with `-e`).
