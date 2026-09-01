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

# 2. (Optional) enable account features
export MDL_USERNAME=your_username
export MDL_PASSWORD=your_password

# 3. Run
uvicorn mdlaw:app --port 8000
# → http://localhost:8000/docs
```

That's it. No API key to configure (the `mdl-api-key` header is an optional
client nonce — see below). No database, no build, no migrations. The default
cache is in-memory.

## Verify it works

```bash
# health + one data endpoint
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/genres

# offline self-check (no network, no server needed)
python3 mdlaw.py self

# full test suite (offline)
python3 -m pytest tests/ -q      # expect: 14 passed
```

## Project layout

```
mdlaw.py            # the entire API (config, client, cache, auth, routes)
tests/test_mdlaw.py # 14 offline checks (no network)
api/index.py        # Vercel serverless entrypoint
vercel.json         # Vercel config
Dockerfile          # python:3.12-slim, non-root
fly.toml            # Fly.io config
requirements.txt    # fastapi, uvicorn[standard], httpx, curl_cffi (+ optional DB drivers)
.env.example        # all env vars documented
pytest.ini          # pythonpath = .
```

## Environment variables

| Var | Purpose | Required | Default |
|---|---|---|---|
| `MDL_API_KEY` | Pin a fixed `mdl-api-key` (client nonce — optional, no security meaning) | no | generated nonce |
| `MDL_TRANSPORT` | `curl_cffi` (TLS impersonation, default) or `httpx` (plain TLS) | no | `curl_cffi` |
| `MDL_USERNAME` + `MDL_PASSWORD` | Enable auth-gated endpoints (title detail, search, watchlist, /me) | no | disabled |
| `MDL_CACHE_BACKEND` | `memory` \| `sqlite` \| `mysql` \| `postgres` | no | `memory` |
| `MDL_CACHE_DB_URL` | DSN for SQL backends, e.g. `sqlite:///mdlaw_cache.db` | only if backend ≠ memory | — |

Copy `.env.example` to `.env` and fill in values; `mdlaw` reads env vars at
import time (no dotenv loader — set them in the shell or a process manager).

## How it works (the 30-second version)

- **Upstream**: the MDL Android app's JSON API behind Cloudflare.
  `default_headers()` reproduces the app's header scheme
  (`User-Agent: okhttp/4.12.0`, `mdl-api-key`, `X-Client-*`).
- **`mdl-api-key` is NOT a secret**: it's a 20-char client nonce the app
  generates per launch; the server never validates it (verified live — no
  header, real key, and random values all return 200). `mdlaw` generates one
  automatically.
- **Transport**: default is `curl_cffi` (browser/mobile TLS impersonation —
  passes Cloudflare's JA3/JA4 challenge from flagged/datacenter IPs). Set
  `MDL_TRANSPORT=httpx` for a plain TLS stack (lighter; works from most
  residential IPs). `curl_cffi` is installed by default.
- **Throttle**: outbound is limited (2 concurrent, 0.5 s min interval) — the
  WAF soft-blocks bursts. Never remove this.
- **Cache**: `TTLCache` (memory) or `SQLCache` (sqlite/mysql/postgres). Stored
  responses carry a sha256 hash; on TTL expiry the upstream is re-fetched and
  the hash is compared → `changed=1` if the response actually changed.
- **Auth**: logs in once with env credentials (`POST /auth/login`, password
  MD5-hashed), decodes JWT `exp`, refreshes before expiry, re-logs in if
  refresh fails. 2FA is NOT supported (returns 428).
- **Genres & languages are reference lists, not filters**: `GET /genres` and
  `GET /languages/supported?v=2` return full catalogs. Search is
  `POST /search/titles?edge=1&q=<q>&page=<page>&synopsis=1` — `q`/`page` are
  URL query params, NOT a JSON body — GET → 405. Search items carry
  `country`/`language`/`type`/`media_type`/`year` but no `genres` field; only
  `title(id)` detail has `genres[]`. No server-side browse/filter endpoint
  exists (404/405). `MDL.search()` post-filters client-side on those fields;
  `MDL.browse_by_genre()` fetches details and filters by genre (1 upstream
  call per candidate — keep limit small).

## Conventions & pitfalls

- **Do not remove the throttle or the cache** — that's what keeps us under the
  WAF. A burst of uncached requests → soft 404s.
- **Don't change the header scheme** unless MDL updates the app. If endpoints
  start 403ing, the constants at the top of `mdlaw.py` are the single place to
  update.
- **No API key in source**: `mdlaw.py` must never embed a fixed key or
  obfuscation machinery. `API_KEY` comes from `MDL_API_KEY` env or a generated
  nonce — nothing to leak.
- **Run `python3 mdlaw.py self`** before committing — it's a fast offline
  sanity check (headers, cache, MD5 formula, SQL change detection).
- **Keep tests offline** — `tests/` must never hit the network.
- **Single-file rule** — new endpoints go in `mdlaw.py` unless there's a real
  reason to split. Avoid adding dependencies; `fastapi` + `httpx` + `curl_cffi`
  + stdlib cover 99% of needs. Optional DB drivers (`pymysql`, `psycopg`) stay
  optional.
- **Auth-gated endpoints** need `auth=True` in the `fetch()` call; without
  credentials they return a clear 400 (handled centrally in `fetch`).
- **CLI session**: `mdlaw auth` saves to `~/.mdlaw_auth.json` (chmod 600);
  `MDL()` and later CLI calls auto-load it. Don't commit that file.

## Deploy

- **Fly.io** (recommended): `fly.toml` included → `fly launch --no-deploy`,
  `fly secrets set MDL_USERNAME=... MDL_PASSWORD=...`, `fly deploy`.
- **Vercel**: `api/index.py` + `vercel.json` included. Note: cold starts
  (~2–5 s) and per-instance ephemeral cache — fine for demos, not for
  latency-critical use.
- **Docker**: `docker build -t mdlaw . && docker run -p 8000:8000 mdlaw`
  (pass env vars with `-e`; all optional).
