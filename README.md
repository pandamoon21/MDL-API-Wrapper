# mdlaw

![PyPI](https://img.shields.io/pypi/v/mdlaw)
[![PyPI Downloads](https://static.pepy.tech/badge/mdlaw)](https://pepy.tech/projects/mdlaw)
![GitHub Repo stars](https://img.shields.io/github/stars/pandamoon21/MDL-API-Wrapper?style=social)
![GitHub forks](https://img.shields.io/github/forks/pandamoon21/MDL-API-Wrapper?style=social)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

**Blazing-fast unofficial API for [MyDramaList](https://mydramalist.com)**, powered by the **official MDL Android app API** (`app-api.mydramalist.com/v1`).

Built from reverse-engineering `com.mydramalist.app` v2.3.18 (Flutter/Dio): the app's request header scheme, auth flow, and endpoint inventory were recovered and live-validated before this wrapper was written.

> ⚠️ **Unofficial.** Not affiliated with MyDramaList. For research & personal use. Stop using if an official API ships.

---

## ✨ Why mdlaw?

| | Web scrapers | **mdlaw** |
|---|---|---|
| Data source | HTML scraping (`mydramalist.com`) | **Official app JSON API** |
| Speed | Slow — parses HTML per request | **~1 ms** after cache warm-up |
| Fragility | Breaks when site markup changes | Stable — same API the app uses |
| WAF risk | High | Throttled + cached outbound |

The Android app talks to a JSON API behind Cloudflare. `mdlaw` replicates the exact request headers the app sends, so it gets **real structured data** — not scraped HTML.

## 📊 Real-world benchmark (measured, not estimated)

Same machine, same network, live hits (median of 3 runs):

| Test | Latency | Payload |
|---|---|---|
| **mdlaw** `GET /api/v1/genres` (cached) | **2.9 ms** | 35 genres · 1.7 KB JSON |
| **mdlaw** `GET /api/v1/titles/1/reviews` (cold — hits upstream) | **298 ms** | 5 reviews JSON |
| **Web scraper** fetch `mydramalist.com/` + parse HTML | **395 ms** | 112 KB HTML |

**Result: mdlaw (cached) is ~136× faster** than a typical web scraper, and even a *cold* mdlaw hit (real upstream call) beats HTML scraping. On top of that, scrapers return HTML that you still have to parse, while mdlaw hands you clean JSON.

> Benchmark: 2026-09-01, `python3` + `urllib` on localhost → mydramalist.com. Your numbers will vary with network, but the order of magnitude is the point.

---

## 🤖 AI-agent instructions

`AGENTS.md` is a purpose-built instruction file for AI coding agents (and new
developers): how to install, verify, extend, and deploy `mdlaw` without
tripping on the WAF/throttle/cache rules.

- **File**: [`AGENTS.md`](./AGENTS.md)
- It covers: quick start, verification commands, env var reference, how the
  throttling/cache/auth works, conventions & pitfalls (never remove the
  throttle, keep tests offline, don't leak credentials), and deploy steps.

Point any agent (or your future self) at `AGENTS.md` first — it's the fastest
path to a working setup.

---

## 🚀 Quickstart

**Option 1 — pip (PyPI):**

```bash
pip install mdlaw
mdlaw                                # serves http://0.0.0.0:8000 (Swagger at /docs)
mdlaw self                           # offline self-check
mdlaw genres                         # one-shot JSON data commands (see below)
```

> `mdl-api-key` is **not a secret** — it's a client nonce the MDL app generates
> per launch, and the server never validates it (verified live). `mdlaw`
> generates one for you; set `MDL_API_KEY` only to pin a value for reproducible
> requests.

**Option 2 — from source:**

```bash
# 1. Clone & enter
git clone <your-repo-url> mdlaw && cd mdlaw

# 2. Create venv & install
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. Run
uvicorn mdlaw:app --port 8000
```

Verify:

```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok","service":"mdlaw"}

curl http://localhost:8000/api/v1/genres | python3 -m json.tool
```

Interactive docs (Swagger UI): **http://localhost:8000/docs**

## 🖥 CLI

`mdlaw` is also a one-shot CLI — print JSON directly to the terminal (no
server needed). Works after `pip install mdlaw`:

```bash
mdlaw --help                     # show all commands
mdlaw auth                       # log in once, save session to ~/.mdlaw_auth.json
mdlaw auth you@example.com hunter2   # one-liner login, no prompts
mdlaw auth status                # show saved session (user, token expiry)
mdlaw logout                     # remove the saved session
mdlaw genres                     # all genres
mdlaw search "crash landing"     # search titles (requires auth)
mdlaw search-people "lee minho"  # search people (requires auth)
mdlaw title 686                  # title detail (requires auth)
mdlaw people 12345               # actor/crew profile
mdlaw watchlist completed        # your watchlist (requires auth)
mdlaw watchlist                  # full watchlist (requires auth)
mdlaw me                         # your profile (requires auth)
mdlaw leaderboard weekly         # period: alltime | weekly | monthly
mdlaw languages                  # supported languages
mdlaw calendar                   # upcoming episodes
```

Output is pretty-printed JSON. Auth-gated commands need a session — either
log in once with `mdlaw auth` (one-liner `mdlaw auth <user> <pass>` or
prompts; saves to `~/.mdlaw_auth.json` chmod 600, then **reuses +
auto-refreshes the token**), or set `MDL_USERNAME`/`MDL_PASSWORD` env vars:

```bash
mdlaw auth you@example.com hunter2   # one-liner, no prompts
mdlaw me                         # uses the saved token — no env needed
MDL_USERNAME=you@example.com MDL_PASSWORD=hunter2 mdlaw search "crash landing"
```

The CLI defaults to the `curl_cffi` transport (passes Cloudflare from flagged
IPs); override with `--transport httpx` if you prefer.

Errors print a clean `error: ...` message to stderr with exit code 1 — no
tracebacks.

## 🐍 Use as a Python package

`mdlaw` works as a library too — no server needed. All methods are async and
share the same cache, throttle, auth and transport as the HTTP API:

```python
import asyncio
from mdlaw import MDL

async def main():
    mdl = MDL()                      # optional: MDL(transport="curl_cffi")

    genres = await mdl.genres()      # list of genres
    title = await mdl.title(686)     # title detail (requires account credentials)
    results = await mdl.search("crash landing")   # search titles
    watch = await mdl.watchlist("completed")      # your watchlist (requires credentials)
    me = await mdl.me()              # your profile (requires credentials)

    print(mdl.stats())               # {'backend': 'TTLCache', 'hits': ..., ...}
    await mdl.close()                # close the HTTP session

asyncio.run(main())
```

### Authenticating as a package

Account endpoints (`title()`, `search()`, `watchlist()`, `me()`, …) need
credentials. Three ways — same auto-login + auto-refresh behavior:

**1. Constructor (recommended for library use):**

```python
mdl = MDL(username="you@example.com", password="hunter2")
me = await mdl.me()     # logs in automatically on first auth-gated call
```

**2. Env vars** (set before `import mdlaw`, or in your process manager):

```bash
MDL_USERNAME=you@example.com MDL_PASSWORD=hunter2 python app.py
```

**3. Saved CLI session** — if you already ran `mdlaw auth`, the constructor
reuses `~/.mdlaw_auth.json` automatically (no credentials needed):

```python
mdl = MDL()             # auto-loads the session saved by `mdlaw auth`
me = await mdl.me()     # uses the saved token, auto-refreshes when expired
```

> ⚠️ Env vars are read at import time. Setting `os.environ[...]` after
> `import mdlaw` has no effect — use the constructor instead for dynamic
> credentials. Tokens live in an in-memory `_auth` dict while the process
> runs; `mdlaw auth` additionally persists them to `~/.mdlaw_auth.json`
> (chmod 600) so CLI and library calls across processes reuse them. Both
> paths share the token cache, so calling `me()` twice logs in once. 2FA
> accounts are not supported (428).

Every `MDL` method maps to a route: `genres()`, `languages()`, `calendar()`,
`articles_featured()`, `lists_featured()`, `lists_popular()`, `leaderboard()`,
`people()`, `payment_plans()`, `payment_coins()`, `title()`,
`title_reviews()`, `title_recommendations()`, `title_comments()`,
`title_credits()`, `search()`, `search_people()`, `watchlist()`, `me()`.

Prefer `await mdl.get(path, ttl=..., auth=...)` / `post(...)` for ad-hoc calls
to endpoints that aren't wrapped yet. Account endpoints require
`MDL_USERNAME` + `MDL_PASSWORD` env vars, exactly like the HTTP server.

## 🎯 Genres & languages — reference data (not filters)

`genres()` and `languages()` are **reference lists**, not query filters. They
return every genre / language MyDramaList knows, which is useful for building
dropdowns, badges, or localized UI. They are **not** accepted as parameters by
`search()` or `title()` — the upstream API ignores them (verified live, see
below).

**`await mdl.genres()`** → list of 35 genres:

```python
[
  {"id": 1, "name": "Action", "slug": "action"},
  {"id": 5, "name": "Adventure", "slug": "adventure"},
  {"id": 13, "name": "Business", "slug": "business"},
  # ... 35 total
]
```

**`await mdl.languages()`** → dict with `languages[]` (39 supported):

```python
{
  "country": "ID",
  "default_language": {"mobile": "en-US", "web": "en-US"},
  "language": "id",
  "languages": [
    {"id": 462, "code": "ar", "iso_code": "SA", "name": "Arabic",
     "native_name": "Arabic", "i18n": false, "i18n_web": false},
    # ... 39 total
  ]
}
```

### Can I filter `search()` / `title()` by genre or language?

**Server-side: no.** The upstream API has no browse/discover/filter endpoint —
every candidate path (`/titles/browse`, `/discover`, `/titles/filter`, …) is
404/405, and `GET /titles/{id}?genre_id=1` / `?genre=1` / `?language_id=462`
silently ignores the params. `mdlaw` filters client-side instead (below).

### Search works — correct endpoint

`search()` (and the `/api/v1/search` route) hits **`POST /search/titles?edge=1&q=<q>&page=<page>&synopsis=1`** — `q` and `page` are **URL query params**, not a JSON body. Verified live:

```bash
mdlaw search "crash landing on you"
# → "Crash Landing on You" (2019), "Crash Landing on You Special...", ...
```

Earlier builds posted `{"q": ...}` to `/search`, which the server ignored
(returning a default feed) — fixed in 1.5.1.

### Client-side filtering — `search()` post-filters + `browse_by_genre()`

Search results carry `country`, `language`, `type`, `media_type`, and `year`
(but **no `genres` field**), so `search()` post-filters on those:

```python
await mdl.search(country="South Korea")                 # only Korean results
await mdl.search(type="Drama", year=2024)               # Dramas from 2024
await mdl.search(language="Chinese", media_type="Movie")  # Chinese movies
await mdl.search(limit=5)                               # cap at 5 items
```

`q` is kept for API compatibility but does nothing upstream.

Genre isn't filterable from search results, so `browse_by_genre()` fetches each
candidate's detail (the only place `genres[]` appears) and keeps those matching:

```python
await mdl.browse_by_genre(1)                    # genre_id 1 = Action
await mdl.browse_by_genre(13, source="top_movies", limit=5)
# source: "search" (default feed) | "trending" | "top_movies"
# each candidate = 1 upstream title-detail call — keep limit small
```

### Where genre/language data *does* appear

| Source | Genre | Language |
|---|---|---|
| `genres()` / `languages()` | ✅ full list | ✅ full list |
| `title(id)` detail (`?expand=1`) | ✅ `genres[]` (`{id, name, slug, ...}`) | ✅ `language` + `country` |
| `search(q)` results | ❌ none | ✅ `language`, `country` |
| `calendar()`, `trending`, `top_movies` | ❌ none | ✅ `country`, `language` |

Practical pattern: use `genres()` to build a picker UI, `search()` to browse
with country/language/type/year post-filters, and `browse_by_genre()` (or
iterate `title(id)` details) when you specifically need genre filtering.

## 📚 API docs & playground

`mdlaw` ships with the industry-standard docs out of the box (FastAPI built-ins — no extra setup):

| URL | What it is |
|---|---|
| **`/`** | Redirects to Swagger UI |
| **`/docs`** | **Swagger UI — interactive playground.** "Try it out" on any endpoint, fill params, execute live, see the JSON response + latency. |
| **`/redoc`** | ReDoc — clean, readable reference docs |
| **`/openapi.json`** | Machine-readable OpenAPI 3.1 spec (for codegen / clients) |

The Swagger playground is pre-configured for quick use: *Try it out* is open by default, request duration is displayed, and the schemas section is collapsed so endpoints are front and center.

## 🖥 Deploy locally

Three options, same code.

### Option 1 — Run directly (dev)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn mdlaw:app --host 0.0.0.0 --port 8000
# → http://localhost:8000/docs
```

### Option 2 — Docker

```bash
docker build -t mdlaw .
docker run -p 8000:8000 mdlaw
```

### Option 3 — Docker Compose (production-ish)

```yaml
# docker-compose.yml
services:
  mdlaw:
    build: .
    ports:
      - "8000:8000"
    environment:
      - MDL_USERNAME=${MDL_USERNAME}   # optional
      - MDL_PASSWORD=${MDL_PASSWORD}   # optional
    restart: unless-stopped
```

```bash
docker compose up -d
```

---

## 📡 Endpoints

All responses are the **raw upstream JSON** (fastest path, zero transformation).

| Method | Route | Upstream | Cache TTL | Auth |
|---|---|---|---|---|
| GET | `/api/v1/health` | — | — | — |
| GET | `/api/v1/dashboard` | — *(local)* | — | — |
| GET | `/api/v1/status` | — *(alias dashboard)* | — | — |
| GET | `/api/v1/auth/status` | — *(local)* | — | — |
| GET | `/api/v1/cache/stats` | — *(local)* | — | — |
| POST | `/api/v1/auth/login` | `POST /auth/login` *(force re-login)* | — | env |
| POST | `/api/v1/auth/refresh` | `POST /auth/refresh` *(force refresh)* | — | env |
| GET | `/api/v1/genres` | `GET /genres` | 1h | — |
| GET | `/api/v1/languages` | `GET /languages/supported?v=2` | 1h | — |
| GET | `/api/v1/titles/{id}` | `GET /titles/{id}?expand=1` | 5m | ✅ |
| GET | `/api/v1/titles/{id}/reviews` | `GET /titles/{id}/reviews` | 5m | — |
| GET | `/api/v1/titles/{id}/comments` | `GET /titles/{id}/comments` | 5m | — |
| GET | `/api/v1/titles/{id}/credits` | `GET /titles/{id}/credits` | 5m | — |
| GET | `/api/v1/titles/{id}/recommendations` | `GET /titles/{id}/recommendations` | 10m | ✅ |
| POST | `/api/v1/search?q={q}&page={page}` | `POST /search/titles?edge=1&q={q}&page={page}&synopsis=1` | 5m | ✅ |
| POST | `/api/v1/search/people?q={q}&page={page}` | `POST /search/people?q={q}&page={page}` | 5m | ✅ |
| GET | `/api/v1/watchlist` | `GET /sync/mylist/watchlist` | 1m | ✅ |
| GET | `/api/v1/watchlist/{status}` | `GET /sync/mylist/{status}` | 1m | ✅ |
| GET | `/api/v1/me` | `GET /users/me` | 1m | ✅ |
| GET | `/api/v1/calendar` | `POST /calendar/episodes` | 1h | — |
| GET | `/api/v1/articles/featured?page={page}` | `GET /articles/featured` | 10m | — |
| GET | `/api/v1/lists/featured?limit={limit}` | `GET /lists/featured` | 10m | — |
| GET | `/api/v1/lists/popular?limit={limit}` | `GET /lists/popular_voting_lists` | 10m | — |
| GET | `/api/v1/people/leaderboard?period={alltime\|weekly\|monthly}` | `GET /people/leaderboard` | 10m | — |
| GET | `/api/v1/people/{id}` | `GET /people/{id}` | 24h | — |
| GET | `/api/v1/payment/plans` | `GET /payment/plans` | 1h | — |
| GET | `/api/v1/payment/coins` | `GET /payment/coins` | 1h | — |

> **Auth ✅** = needs `MDL_USERNAME` + `MDL_PASSWORD` (see [🔐 Account](#-account)). Without them these return `400` with a clear message.

### Example responses (captured live)

**`GET /api/v1/genres`** → `200` · `Cache-Control: public, max-age=3600`

```json
[
  { "id": 1, "name": "Action", "slug": "action" },
  { "id": 5, "name": "Adventure", "slug": "adventure" },
  { "id": 13, "name": "Business", "slug": "business" }
]
```

**`GET /api/v1/languages`** → `200` · `Cache-Control: public, max-age=3600`

```json
{
  "country": "ID",
  "default_language": { "mobile": "en-US", "web": "en-US" },
  "language": "id",
  "languages": [
    { "id": 462, "code": "ar", "iso_code": "SA", "name": "Arabic",
      "native_name": "Arabic", "i18n": false, "i18n_web": false }
  ]
}
```

> ℹ️ **Genres & languages are reference lists, not search filters.** The
> upstream `POST /search` ignores `genre_id` / `language_id` / `genres` /
> `languages` / `page` / `limit` / `sort` (verified live) — it always returns
> the same 20 default results. See [🎯 Genres & languages](#-genres--languages--reference-data-not-filters).

**`GET /api/v1/people/6997`** → `200`

```json
{
  "id": 6997,
  "name": "Shin Myung Jin",
  "first_name": "Myung Jin",
  "family_name": "Shin",
  "biography": "A South Korean actor. Starred in the movie December (2014).",
  "permalink": "https://mydramalist.com/people/6997-shin-myung-jin",
  "nationality": "South Korean",
  "ranked": 99999,
  "images": {
    "thumb": "https://i.mydramalist.com/QDldg_5t.jpg",
    "medium": "https://i.mydramalist.com/QDldg_5m.jpg",
    "poster": "https://i.mydramalist.com/QDldg_5c.jpg"
  }
}
```

**`GET /api/v1/titles/1/reviews`** → `200`

```json
[
  {
    "id": 3561,
    "ratings": { "story": 10, "acting": 10, "music": 10, "rewatch": 10, "overall": 10 },
    "headline": "As I watched through the whole...",
    "upvotes": 0,
    "total_votes": 0,
    "spoiler": false,
    "lang_iso": "en"
  }
]
```

**`GET /api/v1/calendar`** → `200` — airing calendar

```json
{
  "items": [
    {
      "id": 4349807, "rid": 695673, "episode_number": 19,
      "released_at": 1788141600, "duration": 19,
      "permalink": "/695673-wu-zuo-nu-fu-ma/episode/19"
    }
  ],
  "relationships": []
}
```

### Errors

| Case | Status | Body |
|---|---|---|
| Upstream 4xx (e.g. auth-gated) | `400`/`401`/`403` | `{"error": true, "code": <status>, "detail": <upstream body>}` |
| Upstream timeout | `504` | `{"error": true, "code": 504, "detail": "upstream timeout"}` |
| Upstream unreachable | `502` | `{"error": true, "code": 502, "detail": "upstream error: ..."}` |
| Invalid query param | `400` | `{"error": true, "code": 400, "detail": "period must be alltime\|weekly\|monthly"}` |
| Bad path / type | `404` / `422` | FastAPI default |

---

## ⚡ Performance

- **Async + connection pooling** — one `httpx.AsyncClient` / `curl_cffi.AsyncSession` reuses TCP/TLS connections.
- **TTL cache in-memory** — repeat requests never hit upstream. Measured:
  - Cold (first hit): **~0.5 s**
  - Warm (cached): **~1 ms**
- **`Cache-Control` header** on every response → CDN/proxy caching works too.
- **Outbound throttle** (2 concurrent, 0.5 s min interval) — keeps us under MDL's WAF rate limit.

> **Note:** the in-memory cache is single-process. Swap to a SQL backend or Redis if you run more than one instance.

---

## 💾 Response cache (DB)

By default `mdlaw` caches responses **in memory** (TTL). For persistence across restarts / multiple instances, switch to a **SQL database**:

| Env | Value | Notes |
|---|---|---|
| `MDL_CACHE_BACKEND` | `memory` (default) · `sqlite` · `mysql` · `postgres` | `memory` needs no setup |
| `MDL_CACHE_DB_URL` | e.g. `sqlite:///mdlaw_cache.db` · `mysql://user:pass@host/db` · `postgresql://user:pass@host/db` | required when backend ≠ memory |

```bash
# SQLite (zero setup — stdlib driver)
export MDL_CACHE_BACKEND=sqlite
export MDL_CACHE_DB_URL=sqlite:///mdlaw_cache.db
uvicorn mdlaw:app --port 8000

# MySQL
pip install pymysql
export MDL_CACHE_BACKEND=mysql MDL_CACHE_DB_URL=mysql://user:pass@localhost:3306/mdlaw
uvicorn mdlaw:app --port 8000

# PostgreSQL
pip install "psycopg[binary]"
export MDL_CACHE_BACKEND=postgres MDL_CACHE_DB_URL=postgresql://user:pass@localhost:5432/mdlaw
uvicorn mdlaw:app --port 8000
```

The cache table (`mdlaw_cache`) stores each response as JSON + a **sha256 hash**.

### 🔄 How response changes are detected

MDL doesn't push changes — so `mdlaw` uses the standard **TTL + hash comparison** pattern:

1. Response is stored with its sha256 hash.
2. When the TTL expires, the upstream is re-fetched.
3. The new hash is compared to the stored one:
   - **Different** → the row is updated and flagged `changed=1` (with `updated_at`).
   - **Same** → the row is refreshed (TTL extended), `changed=0`.
4. So you always know *when a response actually changed* — not just when it was re-fetched.

Inspect live:

```bash
curl http://localhost:8000/api/v1/cache/stats
# {"backend": "sqlite", "hits": 1, "misses": 1, "hit_rate": 0.5, "entries": 1}
```

> **Note:** change detection is only meaningful for a persistent backend (sqlite/mysql/postgres). In-memory cache resets on restart, so nothing to compare against.

---

## ⚙️ Configuration requirements

Everything `mdlaw` needs is set via environment variables. Here's the full picture at a glance:

| Variable | Required? | What it does | Default |
|---|---|---|---|
| `MDL_API_KEY` | No | Pin a fixed `mdl-api-key` (client nonce; optional, no security meaning) | generated nonce |
| `MDL_TRANSPORT` | No | `curl_cffi` (TLS impersonation, default) or `httpx` (plain TLS) | `curl_cffi` |
| `MDL_USERNAME` | No | Enables auth-gated endpoints (title detail, search, watchlist, `/me`) | disabled |
| `MDL_PASSWORD` | No | Password for the account above (paired with `MDL_USERNAME`) | disabled |
| `MDL_CACHE_BACKEND` | No | `memory` · `sqlite` · `mysql` · `postgres` | `memory` |
| `MDL_CACHE_DB_URL` | Only if backend ≠ memory | DSN, e.g. `sqlite:///mdlaw_cache.db` | — |

### 🔐 API key (optional)

`mdl-api-key` looks like a secret but **isn't one**: it's a 20-character client
nonce that the MDL Android app generates on every launch
(`Utils.getRandomString()` in `main.dart`), and the server **never validates
it** — verified live: requests with no header, with the "real" key, and with a
random value all return `200 OK`. The actual access gate is Cloudflare bot
protection (TLS/JA3 fingerprint), not this header.

So there is **nothing to configure**: `mdlaw` generates a valid nonce for you
and just works. Set `MDL_API_KEY` only if you want reproducible requests.

```bash
uvicorn mdlaw:app --port 8000   # no env needed — works out of the box
```

### 🔀 Transport

The default transport is `curl_cffi`, which impersonates a real
browser/mobile TLS fingerprint (`safari_ios`) and passes Cloudflare's JA3/JA4
bot protection — this is what lets `mdlaw` work from datacenter/flagged IPs
where a plain TLS stack gets a `403 "Just a moment..."` challenge. It is
installed by default (`pip install mdlaw`).

If you're on a normal residential IP and want a lighter stack, switch to
plain `httpx`:

```bash
MDL_TRANSPORT=httpx uvicorn mdlaw:app --port 8000
```

> `httpx` is still a dependency (used for the fallback path); the `curl_cffi`
> package itself is the default client. Both share the same header scheme.

### 👤 Account (optional)

Set both to unlock auth-gated endpoints:

```bash
export MDL_USERNAME=your_username
export MDL_PASSWORD=your_password
uvicorn mdlaw:app --port 8000
```

Without them, auth-gated endpoints return a clear `400` ("requires MDL_USERNAME and MDL_PASSWORD env vars").

### 💾 Cache (optional)

```bash
# default (in-memory) — no setup
uvicorn mdlaw:app --port 8000

# persistent (SQLite, zero dependencies)
export MDL_CACHE_BACKEND=sqlite MDL_CACHE_DB_URL=sqlite:///mdlaw_cache.db
uvicorn mdlaw:app --port 8000
```

---

## 🔐 Account (auth) — how it works

1. `POST /auth/login?device_id=<uuid>` with `{username, password: md5(password)}` → `{token, refresh_token, user}`.
2. Token is a JWT; `exp` is decoded to know when to refresh.
3. On `401` (or `invalid_grant`), `POST /auth/refresh` is called; if that fails, it re-logs in.

Check status:

```bash
curl http://localhost:8000/api/v1/auth/status
# {"configured": true, "logged_in": true, "user": {...}, "expires_at": ..., "refreshes": 0, ...}
```

> ⚠️ **2FA is not supported.** If your account has 2FA enabled, either disable it or use an app password. The wrapper will return `428` ("2FA required") instead of hanging.

---

## 🚢 Deploy

All options below. This is the **public** build: `mdlaw` works with **zero
configuration** — the API key is an optional client nonce, so there is no
required env var.

| Env | Public build |
|---|---|
| `MDL_API_KEY` | optional (pin a nonce for reproducible requests) |
| `MDL_TRANSPORT` | optional (`curl_cffi` default, `httpx` to opt out) |
| `MDL_USERNAME` + `MDL_PASSWORD` | optional (auth-gated endpoints) |
| `MDL_CACHE_BACKEND` + `MDL_CACHE_DB_URL` | optional (persistent cache) |

### Option A — Fly.io (recommended)

```bash
# 1. One-time: create the app (reads fly.toml)
fly launch --no-deploy

# 2. Set secrets (all optional)
fly secrets set MDL_USERNAME=<your-user> MDL_PASSWORD=<your-pass>   # optional
fly secrets set MDL_CACHE_BACKEND=sqlite MDL_CACHE_DB_URL=sqlite:////data/mdlaw_cache.db   # optional

# 3. Deploy
fly deploy
```

`fly.toml` is preconfigured: port 8000, region `sin`, **1 machine always
running** (no cold starts), HTTPS forced.

> **Persistent cache on Fly.io**: the default in-memory cache resets on every
> deploy/restart. Use the SQLite backend with a volume if you want it to
> survive restarts:
>
> ```bash
> fly volumes create mdlaw_data --size 1
> fly secrets set MDL_CACHE_BACKEND=sqlite MDL_CACHE_DB_URL=sqlite:////data/mdlaw_cache.db
> # then add to fly.toml:
> #   [mounts]
> #     source = "mdlaw_data"
> #     destination = "/data"
> fly deploy
> ```

### Option B — Vercel (serverless)

Works, **but know the trade-offs**:

```bash
npx vercel
# when prompted, set env vars in the Vercel dashboard:
#   Settings → Environment Variables → add MDL_USERNAME, MDL_PASSWORD,
#   MDL_CACHE_BACKEND, MDL_CACHE_DB_URL  (all optional; MDL_API_KEY not needed)
```

Two files are already included (`api/index.py` + `vercel.json`). Caveats:

- **Cold starts**: Python serverless functions boot per request — first hit after idle is slow (~2–5 s).
- **Cache is per-instance & ephemeral**: the TTL cache resets between cold starts, so MDL's WAF sees more upstream traffic than on a persistent host. Still throttled (2 concurrent, 0.5 s interval), so it holds up, but it's not "blazing" on first hits.
- **SQL cache not recommended on Vercel**: serverless has no persistent filesystem — use an external MySQL/Postgres if you want persistence.
- Fine for a demo / low-traffic personal API. For consistent speed, use **Fly.io** (persistent instance, warm cache).

### Option C — Docker

```bash
docker build -t mdlaw .

# all env vars optional — no API key needed
docker run -p 8000:8000 \
  -e MDL_USERNAME=<your-user> -e MDL_PASSWORD=<your-pass> \
  -e MDL_CACHE_BACKEND=sqlite -e MDL_CACHE_DB_URL=sqlite:////data/mdlaw_cache.db \
  -v mdlaw_data:/data \
  mdlaw
```

### Option D — Docker Compose (production)

```yaml
# docker-compose.yml
services:
  mdlaw:
    build: .
    ports:
      - "8000:8000"
    environment:
      MDL_USERNAME: ${MDL_USERNAME}      # optional
      MDL_PASSWORD: ${MDL_PASSWORD}      # optional
      MDL_CACHE_BACKEND: ${MDL_CACHE_BACKEND:-memory}
      MDL_CACHE_DB_URL: ${MDL_CACHE_DB_URL:-}
    volumes:
      - mdlaw_data:/data
    restart: unless-stopped

volumes:
  mdlaw_data:
```

```bash
# .env (same folder, gitignored)
MDL_USERNAME=your-user
MDL_PASSWORD=your-pass
MDL_CACHE_BACKEND=sqlite
MDL_CACHE_DB_URL=sqlite:////data/mdlaw_cache.db

docker compose up -d
```

### Option E — VPS / systemd

Any host that can run Python 3.12.

```bash
# /etc/systemd/system/mdlaw.service
[Unit]
Description=mdlaw API
After=network.target

[Service]
WorkingDirectory=/opt/mdlaw
ExecStart=/opt/mdlaw/.venv/bin/uvicorn mdlaw:app --host 0.0.0.0 --port 8000
EnvironmentFile=/opt/mdlaw/.env
Restart=always
User=mdlaw

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mdlaw
# logs: journalctl -u mdlaw -f
```

---

## 🧪 Tests

Offline — no network needed:

```bash
python -m pytest tests/ -q
# 8 passed
```

Covers: key decode, md5 formula, header scheme, TTL cache expiry, SQL cache change detection, route count, auth-offline, and leaderboard validation.

---

## 📦 Project layout

```
mdlaw/
├── mdlaw.py            # the whole API: config, client, cache (memory/SQL), auth, 27 routes
├── pyproject.toml      # PyPI packaging (pip install mdlaw → console script)
├── LICENSE             # MIT
├── AGENTS.md           # AI-agent / dev instructions (install, verify, extend, pitfalls)
├── CHANGELOG.md        # version history (Keep a Changelog convention)
├── api/index.py        # Vercel serverless entrypoint
├── vercel.json         # Vercel config (rewrites, function limits)
├── requirements.txt    # fastapi, uvicorn[standard], httpx, curl_cffi
├── Dockerfile          # python:3.12-slim, non-root
├── docker-compose.yml  # one-command production deploy (with healthcheck)
├── fly.toml            # Fly.io config (port 8000, region sin, always-on)
├── pytest.ini          # pythonpath for tests
├── .env.example        # API key + account + cache backend placeholders
├── tests/
│   └── test_mdlaw.py   # 14 offline checks
└── README.md
```

---

## ⚠️ Known limits

- **2FA accounts** are not supported by the auto-login (returns `428`). Disable 2FA or use an app password.
- **Anonymous guest mode does not exist** — MDL's Firebase project has anonymous auth disabled. Account features need `MDL_USERNAME`/`MDL_PASSWORD`.
- **One account per instance** — the wrapper holds a single token for all requests.
- **Rate limits**: the MDL WAF soft-blocks bursts. `mdlaw` throttles outbound, but don't point heavy crawlers at it.
- **Version drift**: if MDL updates the app's header scheme / key, endpoints may start returning 403. Update `mdlaw.py` constants (single place).

---

## 🛠 Development

```bash
# self-check (offline)
python mdlaw.py self

# run dev server with auto-reload
uvicorn mdlaw:app --reload --port 8000
```

---

## 📜 License & disclaimer

Unofficial, for research/education. Not affiliated with MyDramaList. All data © MyDramaList. Use at your own risk; stop if an official API ships.
