# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
