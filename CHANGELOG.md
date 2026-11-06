# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.7.0] - 2026-11-06

### Security

- `POST /api/v1/auth/login` and `/auth/refresh` returned the internal auth
  state — including the account's access and refresh tokens — to any caller.
  They now require `MDL_ADMIN_TOKEN` (sent as `X-Admin-Token`; unset → `404`)
  and return the same status object as `GET /auth/status`, never tokens.
- Login failure messages no longer echo the raw upstream response.

### Fixed

- Deadlock: `refresh()` held the auth lock and called `login()`, which tried
  to take the same (non-reentrant) lock. Any refresh without a refresh token,
  or a failed refresh, hung every authenticated request.
- Upstream errors with the default `curl_cffi` transport escaped as bare
  `500`s: only httpx exceptions were handled. Timeouts are now `504`,
  transport errors `502`, upstream statuses are passed through, and the
  Cloudflare-challenge hint is shown again.
- Auth calls ignored the upstream HTTP status and crashed on non-JSON bodies.
  A `200` with a non-JSON body is now a `502` instead of a `500`.
- Search/tag/people queries are URL-encoded (`q="a&b"` or `#` no longer
  breaks the request or the cache key).
- A saved CLI session no longer injects fake `"saved"` credentials. When it
  can't be refreshed you get a clear `401` asking you to run `mdlaw auth`.
- The server and library no longer write `~/.mdlaw_auth.json` (it failed on
  read-only filesystems such as Vercel). Only `mdlaw auth`, or a process that
  loaded that file, writes it. A failed write now only warns.
- A refresh response without a `refresh_token`/`user` keeps the previous
  values instead of clearing them.
- MySQL cache backend: schema creation used `Connection.execute` (not in
  pymysql), the reserved word `key` was unquoted, and `TEXT PRIMARY KEY` is
  invalid there. Now: cursor-based schema, quoted `` `key` `` and
  `VARCHAR(768)`. Covered by an offline SQL-capture test; not yet run
  against a live MySQL server.
- `mdlaw serve --port 9000` was parsed as an unknown command. `serve` now
  accepts `--host`/`--port` and honours `HOST`/`PORT`.
- SQL cache change detection never fired in practice: `get()` deleted the
  expired row, so the next `put()` had no previous hash to compare and always
  wrote `changed=0`. Expired rows are now kept, and rows expired for more than
  7 days are pruned every 500 writes.
- Library use across several `asyncio.run()` calls failed with "Event loop is
  closed": the HTTP client and asyncio locks were bound to the first loop. Each
  event loop now gets its own client and locks. Auth tokens and the cache stay
  process-wide.
- Docker: `/data` (the compose volume for the sqlite cache) is now created and
  owned by the non-root `appuser`. Before, it was root-owned and not writable.

### Changed

- SQL cache operations run in a worker thread, so a remote DB no longer
  blocks the event loop.
- Concurrent cache misses for the same key share one upstream request, and a
  burst of `401`s triggers a single token refresh.
- `/cache/stats` and `/dashboard` share one stats implementation; the response
  shape is unchanged.
- The in-memory cache is an LRU bounded by `MDL_CACHE_MAX_ENTRIES` (default
  10000). Each distinct query/page is a new key, so it used to grow without
  limit.
- Route parameters are validated and fail fast with `422`: `page` 1–1000,
  `limit` 1–100, ids ≥ 1, `q` 1–200 characters. The 200-character cap also
  keeps cache keys within the MySQL key column.
- The CLI help no longer says the server defaults to `httpx`.

### Added

- Optional per-IP inbound rate limit: `MDL_RATE_LIMIT` (requests/minute), and
  `MDL_TRUST_PROXY=1` to read the client IP behind Fly or another proxy.
- `.github/workflows/ci.yml` runs ruff and pytest on Python 3.10–3.13. It is
  manual only (`workflow_dispatch`) and never triggers on its own.
- 32 new offline regression tests. A `conftest.py` fixture keeps tests away
  from the real `~/.mdlaw_auth.json`.
- `pyproject.toml`: `dev` extra (`pip install -e .[dev]`), project URLs, and
  ruff config.

## [1.6.2] - 2026-09-02

### Fixed

- False-positive 428 "2FA required" on login. A login response that carries
  both `access_token` and a `challenge_id` (device verification) was wrongly
  rejected — `challenge_id` is NOT 2FA (MDL 2FA is a separate `/auth/2fa/*`
  flow). Login now succeeds whenever a token is present; 428 is raised only
  for genuine 2FA markers (`requires_2fa`, `otp_*`, code 428).

## [1.6.1] - 2026-09-02

### Added

- 5 auth-gated title-feed endpoints + `MDL` methods (live verified with a real
  account): `titles/trending`, `titles/top_airing`, `titles/upcoming`,
  `titles/currently_watching`, `titles/top_movies` — each with `?page=` and
  10 min cache.

## [1.6.0] - 2026-09-02

### Added

- New public endpoints + `MDL` methods (live verified, no auth needed):
  - `GET /api/v1/people/{id}/credits` → `MDL.people_credits(pid)`
  - `GET /api/v1/users/{id}` → `MDL.user(uid)`
  - `GET /api/v1/users/{id}/stats` → `MDL.user_stats(uid)`
  - `GET /api/v1/tags/search?q={q}` → `MDL.search_tags(q)`

### Fixed

- Upstream responses with non-UTF-8 bytes (e.g. Arabic display names on user
  profiles served as Windows-1252) no longer crash with `500` — the shared
  `fetch()` path now decodes leniently (`utf-8` + `errors="replace"`).

## [1.5.6] - 2026-09-02

### Added

- CLI search pagination: `mdlaw search "q" --page 2` and
  `mdlaw search-people "name" --page 2` (was fixed to page 1).
- Prebuilt Docker images published to GHCR on release tags
  (`ghcr.io/pandamoon21/mdl-api-wrapper`, linux/amd64 + linux/arm64) via
  `.github/workflows/docker.yml`.

### Changed

- `requirements.txt` now pins exact versions (`fastapi==0.141.1`,
  `uvicorn==0.52.4`, `httpx==0.28.1`, `curl_cffi==0.16.2`) for reproducible
  deploys. `pyproject.toml` keeps `>=` bounds for library consumers.

## [1.5.5] - 2026-09-02

### Fixed

- `/docs` and `/openapi.json` now report the real package version. The
  FastAPI app had `version="1.0.0"` hardcoded while `__version__` advanced —
  Swagger/OpenAPI always showed 1.0.0. Now `version=__version__`.
- AGENTS.md test count updated (14 → 18).

## [1.5.4] - 2026-09-02

### Added

- `mdlaw auth` is now a one-liner: `mdlaw auth <user> <pass>` logs in with no
  prompts. It also reads `MDL_USERNAME`/`MDL_PASSWORD` env vars, and only
  falls back to interactive prompts when both are missing.

## [1.5.3] - 2026-09-02

### Fixed

- `mdlaw auth status` now shows the account name. The MDL login response
  does not include a `user` object, so `_auth["user"]` was always `None` and
  status printed an empty `logged in as:`. The CLI now stores the login
  `username` in the session file and falls back to it.

## [1.5.2] - 2026-09-02

### Changed

- Cleaned up internal comments/docs to state API facts directly (no
  research-process references).

## [1.5.1] - 2026-09-02

### Fixed

- **Search actually searches now.** The correct upstream endpoint is
  `POST /search/titles?edge=1&q=<q>&page=<page>&synopsis=1` — `q`/`page` are
  URL query params, not a JSON body. The previous build posted `{"q": ...}` to
  `/search`, which the server ignored (always returning a default feed).
  `mdlaw search "crash landing on you"` now returns "Crash Landing on You"
  (verified live); `search_people` → `POST /search/people?q=<q>&page=<page>`
  returns real people ("lee jong" → Lee Jong Suk).
- HTTP routes `/api/v1/search` and `/api/v1/search/people` now accept an
  optional `page` param and forward the corrected path.

## [1.5.0] - 2026-09-02

### Added

- **`MDL.search()` post-filters** — accepts `country`, `language`, `type`,
  `media_type`, `year`, `limit`, and `page`. Client-side filtering because
  upstream has no server-side filter endpoint.
- **`MDL.browse_by_genre(genre_id, limit=10, source=...)`** — fetches
  candidate titles (search / trending / top_movies), loads each detail, and
  returns only those whose `genres[]` include the requested genre_id.
  One upstream title-detail call per candidate — keep `limit` small.

### Changed

- Documentation updated with live-verified findings (2026-09): no
  browse/discover/filter endpoint exists (404/405); search results carry
  `country`/`language`/`type`/`media_type`/`year` but no `genres` field.

## [1.4.0] - 2026-09-02

### Added

- **CLI auth session**: `mdlaw auth` prompts for credentials, logs in, and
  saves the session to `~/.mdlaw_auth.json` (chmod 600). Later CLI commands
  and `MDL()` library calls reuse the saved token and auto-refresh it.
  `mdlaw auth status` shows the saved session; `mdlaw logout` removes it.
- **CLI help**: `mdlaw -h` / `mdlaw --help` lists all commands and options.
- `MDL()` constructor auto-loads the session saved by `mdlaw auth` when no
  env credentials are set.

### Changed

- **`curl_cffi` is now the default transport** everywhere (CLI, library,
  server) — browser/mobile TLS impersonation passes Cloudflare's JA3/JA4
  challenge from flagged/datacenter IPs. It is now a required dependency
  (`pip install mdlaw` includes it). Set `MDL_TRANSPORT=httpx` to use a
  plain httpx TLS stack.
- CLI defaults to `curl_cffi` unless `MDL_TRANSPORT` or `--transport` says
  otherwise.

### Fixed

- Cloudflare "Just a moment..." 403 challenge now returns a clear,
  actionable message (suggests the default `curl_cffi` transport) instead of
  dumping raw HTML.

## [1.3.0] - 2026-09-02

### Added

- **Python package layer**: new `MDL` class — use `mdlaw` as an async library
  without the HTTP server (`from mdlaw import MDL; await mdl.genres()`). All
  methods share the same cache, throttle, auth and transport. See README
  "Use as a Python package".
- `MDL(username=..., password=...)` — authenticate as a library directly in
  the constructor (alternative to `MDL_USERNAME`/`MDL_PASSWORD` env vars,
  which are read at import time). Auto-login + auto-refresh unchanged.
- **One-shot CLI data commands**: `mdlaw genres`, `mdlaw search "q"`,
  `mdlaw title 686`, `mdlaw watchlist`, `mdlaw me`, `mdlaw leaderboard`, … —
  print pretty JSON to stdout without starting a server. Clean `error: ...`
  to stderr + exit code 1 on failure (no tracebacks).

### Changed

- `mdl-api-key` is now treated as the client nonce it actually is: no key
  required (a 20-char nonce is generated automatically), `MDL_API_KEY` only
  pins a value for reproducible requests. Server starts with zero config.
- New optional `MDL_TRANSPORT=curl_cffi` transport — browser/mobile TLS
  impersonation that passes Cloudflare's JA3/JA4 challenge from
  flagged/datacenter IPs (`pip install curl_cffi`).

## [1.0.0] - 2026-09-01

### Added

- FastAPI wrapper over the official MyDramaList Android app API (`app-api.mydramalist.com/v1`).
- 13 public data endpoints: genres, trending, popular, rankings, title details, reviews, people, leaderboard, etc.
- Single-file `mdlaw.py` design — no build step, no database required.
- TTL response cache (in-memory) with `Cache-Control: public, max-age=3600`.
- Request throttle (2 concurrent, 0.5 s min interval) to stay under the MDL WAF.
- Health endpoint (`/api/v1/health`), self-check CLI (`python3 mdlaw.py self`).
- Health dashboard (`/api/v1/dashboard`) + status alias (`/api/v1/status`): uptime, cache hit/miss stats, key source, route list.
- Swagger UI at `/docs` (with Try-it-out playground), ReDoc at `/redoc`, OpenAPI JSON at `/openapi.json`; root `/` redirects to `/docs`.
- Single-account auth via `MDL_USERNAME` + `MDL_PASSWORD` env vars: auto-login, auto-refresh on 401, refresh-before-expiry (60 s buffer), 2FA detection (HTTP 428).
- Auth-gated endpoints: title detail, search, search people, watchlist, watchlist by status, `/me`, recommendations, credits.
- Pluggable response cache backend (`MDL_CACHE_BACKEND`): `memory` (default), `sqlite`, `mysql`, `postgres` (`MDL_CACHE_DB_URL`).
- Cache change detection: sha256 hash of stored JSON compared on TTL expiry → `changed=1` when upstream response actually changed.
- Cache stats endpoint (`/api/v1/cache/stats`): backend flavor, hit stats, change-detection count.
- Auth status endpoint (`/api/v1/auth/status`): configured, logged-in, token expiry, refresh count.
- PyPI packaging: single-module distribution (`py-modules = ["mdlaw"]`), console script `mdlaw`, `__version__`, MIT license.
- Vercel serverless support (`api/index.py` + `vercel.json`).
- Fly.io config (`fly.toml`), production Dockerfile (non-root, healthcheck), Docker Compose (persistent cache volume).
- `AGENTS.md` — AI-agent install/verify/extend instructions.

### Changed

- `MDL_API_KEY` is **required** via environment variable (never embedded in source). The server refuses to start without it; `import mdlaw` stays safe without it (enforced in `lifespan`).
- Search endpoints use `POST /search` (JSON body `{"q", "synopsis"}`) — matches the current upstream API.

### Fixed

- Graceful httpx client shutdown on server stop (`lifespan` handler) — no more warnings/hangs on restart.
- `/people/leaderboard` was captured by the dynamic `/people/{pid}` route — static route now declared first.
- Auth-gated routes without credentials return a clear `auth_required` error instead of an ambiguous 400.
