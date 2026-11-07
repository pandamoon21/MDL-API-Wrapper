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
python3 -m pytest tests/ -q      # expect: 94 passed
```

## Project layout

```
mdlaw.py            # the entire API (config, client, cache, auth, routes)
tests/              # 94 offline checks (no network); conftest.py isolates state
.github/workflows/  # docker.yml (GHCR on tags), ci.yml (ruff + pytest, manual only)
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
| `MDL_LOG_LEVEL` | Server log level for the `mdlaw` logger | no | `INFO` |
| `MDL_CACHE_MAX_ENTRIES` | In-memory cache size (LRU) | no | `10000` |
| `MDL_ADMIN_TOKEN` | Enables `POST /api/v1/auth/{login,refresh}` via `X-Admin-Token` | no | routes 404 |
| `MDL_RATE_LIMIT` | Requests/minute per client IP on `/api/v1/*` (health exempt) | no | `0` (off) |
| `MDL_RETRIES` | Retries for transient upstream failures on reads (GET + read-only search POSTs); `0` disables | no | `2` |
| `MDL_TRUST_PROXY` | `1` → client IP from `Fly-Client-IP`/`X-Forwarded-For` | no | off |

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
- **Search & browse**: `POST /search/titles?edge=1&page=<page>` — params are
  URL query params, NOT a JSON body (GET → 405). With `q` it searches; without
  it, it is the catalog browser with server-side filters `types`
  (68 drama, 77 movie, 86 tv-show), `genres` (ids from `/genres`),
  `countries` (1 Japan, 2 China, 3 Korea, 4 HK, 5 Taiwan, 6 Thailand),
  `releases` (year, inclusive) and `sort` (`top`|`popular`), comma-joined for
  several values. Build it with `browse_path()`. `status`/`ratings`/`start`/
  `tags` exist in the app but their formats are unknown — don't expose them
  until verified. Search items have no `genres` field.

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
- **Upstream calls go through `_send()` + `_parse()`** — they map both httpx
  and curl_cffi failures to 502/504 and upstream statuses to HTTPException.
  Don't call `get_client().request()` directly.
- **Query strings**: build them with `_qs(path, **params)` so user input is
  URL-encoded (and the cache key can't be split).
- **Per-event-loop state**: locks, the HTTP client and in-flight tasks live
  in `_state()` (one per running loop), so library users can call
  `asyncio.run()` repeatedly. Don't add module-level asyncio primitives or
  clients; add a field to `_LoopState` instead.
- **The auth lock is not reentrant**: code that already holds
  `_state().auth_lock` calls `_login_locked()`, never `login()`.
- **Route params**: reuse the `Page`/`Limit`/`SearchQ`/`PosId` `Annotated`
  aliases (never share a `Query()`/`Path()` instance between routes).
- **SQL cache keeps expired rows** — change detection compares against them.
  `SQLCache.prune()` drops rows expired > 7 days.
- **Never return `_auth` from a route** — it holds the access/refresh tokens.
- **Public endpoints stay anonymous** (`auth=False`): some (e.g. `/titles/{id}/feed`)
  include a `current_user` object when a token is sent, which would expose the
  operator's account on a public instance. Only pass `auth=True` when upstream
  requires it.
- **The website's API (`mydramalist.com/v1`) is a different API** — its paths
  only sometimes exist on `app-api`. Verify every path on the mobile API before
  wrapping it.
- **Logging**: library code logs only through `log` (`logging.getLogger("mdlaw")`)
  — upstream calls at INFO (WARNING for 5xx/transport errors), cache/throttle at
  DEBUG. Never log tokens or passwords. The CLI (`-v`/`-vv`) and the server
  (`MDL_LOG_LEVEL`) attach handlers; don't `print()` from library code.
- **CLI**: argparse in `_build_parser()`. A new command needs a parser entry, a
  branch in `_call()`, and a renderer in `_RENDERERS` (`_render_generic` is the
  fallback). Output is text on a TTY and JSON when piped, so keep JSON output
  raw. Errors are `_CLIError` or `HTTPException`, explained by `_explain()` as
  `error:` + `hint:`. Exit codes: 1 runtime, 2 usage.
- **CLI session**: `mdlaw auth` saves to `~/.mdlaw_auth.json` (chmod 600);
  `MDL()` and later CLI calls auto-load it. Only the CLI (or a process that
  loaded that file) writes it — the server never puts tokens on disk.
  Don't commit that file. Tests isolate it via `tests/conftest.py`.

## Deploy

- **Fly.io** (recommended): `fly.toml` included → `fly launch --no-deploy`,
  `fly secrets set MDL_USERNAME=... MDL_PASSWORD=...`, `fly deploy`.
- **Vercel**: `api/index.py` + `vercel.json` included. Note: cold starts
  (~2–5 s) and per-instance ephemeral cache — fine for demos, not for
  latency-critical use.
- **Docker**: `docker build -t mdlaw . && docker run -p 8000:8000 mdlaw`
  (pass env vars with `-e`; all optional).
