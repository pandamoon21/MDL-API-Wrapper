# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `py.typed` marker: mypy and pyright now use mdlaw's inline type hints.

### Changed

- CLI text rendering moved from `mdlaw/cli.py` into `mdlaw/render.py`.
  Output is unchanged.
- Tests: the `MDL` library wrappers (`library.py` 76% -> 100%) and the
  renderers are covered offline. 140 -> 384 tests.

### Fixed

- `MDL` library: ids and usernames are escaped as a single path segment, so
  `user_lists("../titles/1")` can no longer reach a different upstream route.
  Empty, `.` and `..` raise 400. (The HTTP server already validated these.)
- `MDL` library: `limit` of `None`, `0` or a negative number now means "no
  cap" in `search` and every `browse_by_genre` source. Before, `search`
  returned everything, `browse_by_genre` returned `[]`, and the trending
  source returned one title (`None` crashed).
- `MDL(transport=...)` is lowercased like `MDL_TRANSPORT`, and an unknown
  value raises `ValueError`. Before, `"CURL_CFFI"` silently fell back to httpx.
- `browse_by_genre` skips non-object candidates and genres instead of raising
  `AttributeError`.
- CLI text output:
  - The calendar no longer shows an unrelated show when an episode has no
    `rid`; it falls back to the permalink.
  - Recommendations no longer crash when `author` is not an object.
  - Profile join dates given as epoch seconds print as a date.
  - A list of objects with no known columns prints a `--json` hint instead of
    Python reprs.
  - A boolean rank is not shown as `#1`.

## [1.9.0] - 2026-11-07

### Added

- Reads retry transient upstream failures: timeouts, connection errors, 429
  and 502/503/504. They retry up to `MDL_RETRIES` times (default 2, `0`
  disables) with exponential backoff and jitter. On 429/503 they honour
  `Retry-After` up to 10s. A longer `Retry-After` gives up at once instead of
  hitting the WAF again early.
  - Retried: GET, plus the read-only POSTs under `/search/` and
    `/calendar/episodes`.
  - Never retried: login, refresh and other POSTs.
  - When upstream is down, an error can take a few seconds longer to come back.
- OpenAPI (`/docs`) documents response schemas for the main routes: title
  detail, search, browse, person, health, status/dashboard, cache stats and
  auth status. The common error body is documented too. The schemas are
  documentation only, and responses are passed through unfiltered.
- Opt-in integration tests for the MySQL and PostgreSQL cache backends
  (`MDL_TEST_MYSQL_URL`, `MDL_TEST_PG_URL`).

### Changed

- `mdlaw.py` is now a package (`mdlaw/`) split into config, cache, client,
  server, library and CLI modules. `from mdlaw import app, MDL`,
  `uvicorn mdlaw:app` and the `mdlaw` command still work. The offline
  self-check is now `python -m mdlaw self`.
- `MDL.browse_by_genre()` raises on 400/401/403/428 (for example, missing
  credentials or a Cloudflare block) instead of silently returning `[]`.
  Other per-title errors are logged and skipped.
- `/api/v1/payment/plans` and `/api/v1/payment/coins` are hidden from the
  OpenAPI schema. They still work.

### Fixed

- SQL cache, PostgreSQL: new tables store times as `DOUBLE PRECISION`.
  `REAL` rounded today's timestamps to about 2-minute steps. To fix an
  existing table, run
  `ALTER TABLE mdlaw_cache ALTER COLUMN expires_at TYPE DOUBLE PRECISION`,
  and do the same for `created_at` and `updated_at`.
- SQL cache, MySQL and PostgreSQL: connections run in autocommit. Before this,
  MySQL reads never saw rows written by other workers, and one failed
  Postgres statement broke every later query.
- SQL cache, MySQL: reconnects after a dropped connection.
- SQL cache, MySQL: URL-encoded usernames and passwords are decoded.
- Narrowed broad `except Exception` handlers in token-expiry parsing and
  upstream error decoding.

## [1.8.0] - 2026-11-06

### Added

- Server-side catalog browse. `POST /search/titles` accepts filters when called
  without `q`: `types`, `genres`, `countries`, `releases` (from year) and
  `sort` (`top`|`popular`). These were recovered from the app's strings and
  verified live.
  - `MDL.browse()`, `GET /api/v1/browse` and `mdlaw browse`.
  - Types and countries take names or ids; genres take names, slugs or ids.
    Each filter accepts several values.
- New endpoints, found by mining the app's strings and probing live (read-only):
  - Public: `GET /api/v1/lists/trending`, `/lists/recent`, `/reviews/{id}`,
    `/articles/{id}`.
  - Account: `GET /api/v1/people/popular`, `POST /api/v1/search/users`,
    `GET /api/v1/users/{id|username}/lists`, `/users/{id|username}/friends`.
  - Each has a matching `MDL` method.
- CLI: `mdlaw lists [trending|recent|popular|featured]`.
- Endpoints found in the website's JavaScript (`mydramalist.com/v1`) that also
  work on the mobile API. All are public and fetched without a token:
  - `GET /api/v1/recommendations`: community "if you liked X, watch Y" picks,
    each with a reason.
  - `/titles/{id}/feed` and `/people/{id}/feed`.
  - `/people/{id}/gifts`: top and recent flower givers.
  - `/articles/categories`.
  - `/groups` and `/groups/{id}`.
  - Each has a matching `MDL` method. The CLI adds `mdlaw recommendations`
    (alias `recs`).

### Changed

- `MDL.browse_by_genre()` uses the server-side genre filter by default: one
  request instead of one title-detail call per candidate, and it returns search
  items. `source="trending"`/`"top_movies"` keep the old detail-based
  behaviour.
- CLI `title` card shows the fields the API actually returns: rank,
  popularity, aired range, episodes × runtime, alternative titles, tags,
  watchers and certification. `99999` (not ranked) is hidden.
- CLI `watchlist` shows status (mapped from `list_id`, verified per list), an
  episode progress like `5/7` and the user's score. It defaults to 50 rows.
- CLI `me` card adds contributions and roles.

### Fixed

- Docs said there was no server-side browse/filter. There is, as above.
- Docs and help said `mdlaw watchlist` without a status shows the full list.
  It shows only what you're currently watching (`/sync/mylist/watchlist`).

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
- CLI: bad arguments (`title abc`, `--page` without a value) give exit code 2
  with a usage hint instead of a Python traceback. `leaderboard` and
  `watchlist` reject unknown periods/statuses instead of silently ignoring
  them.

### Added

- CLI rewrite (argparse) focused on readable output and clear errors:
  - Text tables and cards on a terminal; raw JSON when piped (unchanged for
    scripts). Force either with `--json` / `-o json` or `-o text`, and cap rows
    with `-n`.
  - Dedicated views for genres, languages, calendar (grouped by day, `--days`),
    search (next-page hint), people, title, leaderboard (rank movement),
    profile and watchlist. Unknown shapes fall back to a generic table.
  - Errors are one line plus a hint with the next step (`mdlaw auth`, "check
    the id", …), with no traceback unless `-vv`. "Did you mean" for mistyped
    commands.
  - Exit codes: 1 runtime, 2 usage, 130 Ctrl+C.
  - `-v` logs each upstream request (method, path, status, time, size) and
    auth events. `-vv` adds cache hits/misses and throttle waits.
  - Spinner for slow calls on a TTY.
  - `auth status` shows token state in plain words and has a `--json` form
    without tokens. `auth --password-stdin` keeps passwords out of shell
    history. `search` gains `--country`, `--type` and `--year`.
  - Piped output is UTF-8 on Windows, where CJK titles previously failed to
    encode.
- Structured logging through the `mdlaw` logger. The server prints lines in
  uvicorn's format; set the level with `MDL_LOG_LEVEL` (or `mdlaw serve -v`).
- Optional per-IP inbound rate limit: `MDL_RATE_LIMIT` (requests/minute), and
  `MDL_TRUST_PROXY=1` to read the client IP behind Fly or another proxy.
- `.github/workflows/ci.yml` runs ruff and pytest on Python 3.10–3.13. It is
  manual only (`workflow_dispatch`) and never triggers on its own.
- 59 new offline tests (including `tests/test_cli.py`). A `conftest.py` fixture keeps tests away
  from the real `~/.mdlaw_auth.json`.
- `pyproject.toml`: `dev` extra (`pip install -e .[dev]`), project URLs, and
  a ruff config with an explicit rule set. CI pins the ruff version, so lint
  results don't change when ruff widens its defaults.

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
