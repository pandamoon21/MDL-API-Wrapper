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
mdlaw genres                         # a table on a terminal, JSON when piped
mdlaw people 14352                   # actor profile — no account needed
mdlaw auth                           # log in once (for search, browse, title, lists…)
mdlaw browse --type drama --country korea --sort popular
mdlaw serve                          # HTTP API on http://0.0.0.0:8000 (Swagger at /docs)
mdlaw self                           # offline self-check
```

> `mdl-api-key` is **not a secret** — it's a client nonce the MDL app generates
> per launch, and the server never validates it (verified live). `mdlaw`
> generates one for you; set `MDL_API_KEY` only to pin a value for reproducible
> requests.

**Option 2 — from source:**

```bash
# 1. Clone & enter
git clone https://github.com/pandamoon21/MDL-API-Wrapper mdlaw && cd mdlaw

# 2. Create venv & install
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. Run
uvicorn mdlaw:app --port 8000
```

Verify:

```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok","service":"mdlaw","auth":false}

curl http://localhost:8000/api/v1/genres | python3 -m json.tool

# with MDL_USERNAME/MDL_PASSWORD set:
curl "http://localhost:8000/api/v1/browse?type=drama&genre=romance&country=south-korea&sort=popular"
```

Interactive docs (Swagger UI): **http://localhost:8000/docs**

## 🖥 CLI

`mdlaw` is also a CLI (no server needed). Works after `pip install mdlaw`.
On a terminal it prints readable tables and cards; piped, it prints JSON, so
scripts like `mdlaw genres | jq` keep working.

```bash
mdlaw --help                       # all commands; `mdlaw <command> --help` for details
mdlaw auth                         # log in once (prompts; saves ~/.mdlaw_auth.json, chmod 600)
mdlaw auth status                  # account, token state, session file — never the token
mdlaw logout                       # remove the saved session
mdlaw genres                       # all genres
mdlaw languages                    # supported languages
mdlaw calendar --days 3            # what airs in the next 3 days, grouped by day
mdlaw people 14352                 # actor / crew profile
mdlaw leaderboard weekly -n 10     # top 10 actors this week
mdlaw lists                        # trending user lists (also: recent, popular, featured)
mdlaw recs                         # community picks: "liked X → try Y", with the reason
mdlaw browse --type drama --genre romance --country korea --sort popular
                                   # * server-side catalog browse (--from YEAR, --page N)
mdlaw search "crash landing"       # * search titles (--page, --country, --type, --year)
mdlaw search-people "lee min ho"   # * search actors and crew
mdlaw title 35729                  # * title detail (Crash Landing on You)
mdlaw watchlist                    # * what you're currently watching
mdlaw watchlist completed          # * another list: completed|plantowatch|onhold|dropped|notinterested
mdlaw me                           # * your profile
```

`*` = needs an account (`mdlaw auth`, or `MDL_USERNAME`/`MDL_PASSWORD`). The
saved token is reused and refreshed automatically.

**What it looks like**

```text
$ mdlaw leaderboard weekly -n 3
#  Move  Name           Nationality  Flowers  ID
─  ────  ─────────────  ───────────  ───────  ─────
1  ▲1    Hou Ming Hao   Chinese      5,695    14831
2  ▼1    Chen Zhe Yuan  Chinese      4,895    14352
3  ▲1    Zhao Lu Si     Chinese      2,970    18211

weekly leaderboard · profile: mdlaw people <ID>
showing 3 of 100 · -n 0 shows all

$ mdlaw people 14352
Chen Zhe Yuan (陈哲远)

Nationality  Chinese
Born         1996-10-29
Flowers      946,582
Rank         #1
Link         https://mydramalist.com/people/14352-chen-zhe-yuan

Chen Zhe Yuan is a Chinese actor and singer …
```

**Output options** (before or after the command):

| Option | Effect |
|---|---|
| `--json` / `-o json` | raw upstream JSON (default when piped) |
| `-o text` | tables/cards even when piped |
| `-n N`, `--limit N` | max rows in text output (`0` = all) |
| `-v` | log each upstream request to stderr: `GET /people/14352 -> 200 (344 ms, 1.3 KB)`, plus login/refresh events |
| `-vv` | also cache hits/misses, throttle waits, and full tracebacks |
| `--transport httpx` | use plain httpx instead of `curl_cffi` |

**Errors** go to stderr as one line, plus a hint with the next step. There is
no traceback unless you pass `-vv`:

```text
$ mdlaw title 686
error: saved session expired and could not be refreshed
hint:  log in again with `mdlaw auth`

$ mdlaw serch crash
error: unknown command 'serch'
hint:  did you mean `mdlaw search`?
```

Exit codes: `0` ok · `1` API/network error · `2` bad usage · `130` Ctrl+C.

**Logging in from scripts:** `mdlaw auth <user> <pass>` works, but the
password ends up in your shell history. Prefer the prompt, or:

```bash
printf '%s\n' "$MDL_PASSWORD" | mdlaw auth you@example.com --password-stdin
```

**Server:** `mdlaw serve --host 127.0.0.1 --port 9000`. Running `mdlaw` with
no command also starts it. Defaults come from the `HOST`/`PORT` env vars, else
`0.0.0.0:8000`. Server logs use uvicorn's format; set `MDL_LOG_LEVEL=DEBUG`
(or `mdlaw serve -v`) to see cache activity:

```text
INFO:     mdlaw: transport=curl_cffi cache=memory auth=off
INFO:     mdlaw: GET /genres -> 200 (344 ms, 1.5 KB)
INFO:     127.0.0.1:62054 - "GET /api/v1/genres HTTP/1.1" 200 OK
```

## 🐍 Use as a Python package

`mdlaw` works as a library too — no server needed. All methods are async and
share the same cache, throttle, auth and transport as the HTTP API:

```python
import asyncio
from mdlaw import MDL

async def main():
    mdl = MDL()                      # optional: MDL(transport="curl_cffi")

    genres = await mdl.genres()      # list of genres (public)
    person = await mdl.people(14352) # actor profile (public)
    lists = await mdl.lists_trending(limit=5)       # trending user lists (public)

    # the rest need an account (see "Authenticating as a package")
    title = await mdl.title(35729)   # title detail
    results = await mdl.search("crash landing")    # search titles
    korean_romance = await mdl.browse(types="drama", genres="romance",
                                      countries="south-korea", sort="popular")
    watching = await mdl.watchlist()                # what you're watching now
    done = await mdl.watchlist("completed")         # another list
    me = await mdl.me()              # your profile

    print(mdl.stats())               # {'backend': 'memory', 'hits': ..., ...}
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
`articles_featured()`, `article()`, `articles_categories()`,
`recommendations()`, `title_feed()`, `people_feed()`, `people_gifts()`,
`groups()`, `group()`, `lists_featured()`, `lists_popular()`,
`lists_trending()`, `lists_recent()`, `leaderboard()`, `people()`,
`people_credits()`, `people_popular()`, `user()`, `user_stats()`,
`user_lists()`, `user_friends()`, `review()`, `payment_plans()`,
`payment_coins()`, `title()`, `title_reviews()`, `title_recommendations()`,
`title_comments()`, `title_credits()`, `titles_trending()` and the other title
feeds, `search()`, `browse()`, `search_people()`, `search_users()`,
`search_tags()`, `watchlist()`, `me()`.

Prefer `await mdl.get(path, ttl=..., auth=...)` / `post(...)` for ad-hoc calls
to endpoints that aren't wrapped yet. Account endpoints require
`MDL_USERNAME` + `MDL_PASSWORD` env vars, exactly like the HTTP server.

## 🎯 Browse & filter — server-side

The app's catalog browser is the search endpoint called **without `q`**:
`POST /search/titles?edge=1&page=N` with filter params. (Recovered from the
app's strings and verified live 2026-11-06; earlier versions of these docs said
no server-side filter existed — that was wrong.)

| Filter | Param | Values |
|---|---|---|
| Type | `types` | `drama` (68), `movie` (77), `tv-show` (86) |
| Genre | `genres` | any genre name/slug/id from `genres()` |
| Country | `countries` | `japan` (1), `china` (2), `south-korea` (3), `hong-kong` (4), `taiwan` (5), `thailand` (6) |
| Released from | `releases` | year — that year or later |
| Sort | `sort` | `top` (highest rated, default) or `popular` (most watched) |

Every filter takes several values (comma-joined upstream). 20 results per page;
requires an account.

```python
await mdl.browse(types="drama", genres="romance", countries="south-korea", sort="popular")
await mdl.browse(types=["drama", "movie"], countries=["japan", "thailand"], released_from=2023)
await mdl.browse(genres=[14, "thriller"], types="movie", page=2)
```

```bash
mdlaw browse --type drama --genre romance --country korea --sort popular
curl "localhost:8000/api/v1/browse?type=movie&genre=horror&genre=thriller&from=2020"
```

Not mapped yet: the app also sends `status`, `ratings`, `start` and `tags`
filters, but the values tried had no visible effect, so they are not exposed.

`browse_by_genre(genre_id)` now uses the server-side genre filter (one request,
returns search items). `source="trending"` / `"top_movies"` keep the old
behaviour — one title-detail call per candidate, returning details.

### Search

`search(q)` (and `POST /api/v1/search`) hits **`POST /search/titles?edge=1&q=<q>&page=<page>&synopsis=1`**
— `q` and `page` are URL query params, not a JSON body. It also post-filters
results client-side on the fields search items carry:

```python
await mdl.search("love", country="South Korea", type="Drama", year=2024, limit=5)
```

### Genres & languages — reference lists

`genres()` (35 genres: `{id, name, slug}`) feeds the `genres` browse filter.
`languages()` (39 UI languages) is a reference list only — it is not a
catalog filter.

| Source | Genre | Language / country |
|---|---|---|
| `genres()` / `languages()` | ✅ full list | ✅ full list |
| `title(id)` detail | ✅ `genres[]`, `tags[]` | ✅ `language`, `country` |
| `search()` / `browse()` items | ❌ none (filter server-side instead) | ✅ `language`, `country` |
| `calendar()`, `trending`, `top_movies` | ❌ none | ✅ `country`, `language` |

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

Prebuilt images are published to GHCR on every release tag
(`ghcr.io/pandamoon21/mdl-api-wrapper`, linux/amd64 + linux/arm64):

```bash
docker pull ghcr.io/pandamoon21/mdl-api-wrapper:1.5.6
docker run -p 8000:8000 ghcr.io/pandamoon21/mdl-api-wrapper:1.5.6
```

Or build locally:

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
| POST | `/api/v1/auth/login` | `POST /auth/login` *(force re-login; returns status, never tokens)* | — | `X-Admin-Token` |
| POST | `/api/v1/auth/refresh` | `POST /auth/refresh` *(force refresh; returns status, never tokens)* | — | `X-Admin-Token` |
| GET | `/api/v1/genres` | `GET /genres` | 1h | — |
| GET | `/api/v1/languages` | `GET /languages/supported?v=2` | 1h | — |
| GET | `/api/v1/titles/{id}` | `GET /titles/{id}?expand=1` | 5m | ✅ |
| GET | `/api/v1/titles/trending?page={page}` | `GET /titles/trending` | 10m | ✅ |
| GET | `/api/v1/titles/top_airing?page={page}` | `GET /titles/top_airing` | 10m | ✅ |
| GET | `/api/v1/titles/upcoming?page={page}` | `GET /titles/upcoming` | 10m | ✅ |
| GET | `/api/v1/titles/currently_watching?page={page}` | `GET /titles/currently_watching` | 10m | ✅ |
| GET | `/api/v1/titles/top_movies?page={page}` | `GET /titles/top_movies` | 10m | ✅ |
| GET | `/api/v1/titles/{id}/reviews` | `GET /titles/{id}/reviews` | 5m | — |
| GET | `/api/v1/titles/{id}/comments` | `GET /titles/{id}/comments` | 5m | — |
| GET | `/api/v1/titles/{id}/credits` | `GET /titles/{id}/credits` | 5m | — |
| GET | `/api/v1/titles/{id}/recommendations` | `GET /titles/{id}/recommendations` | 10m | ✅ |
| POST | `/api/v1/search?q={q}&page={page}` | `POST /search/titles?edge=1&q={q}&page={page}&synopsis=1` | 5m | ✅ |
| POST | `/api/v1/search/people?q={q}&page={page}` | `POST /search/people?q={q}&page={page}` | 5m | ✅ |
| POST | `/api/v1/search/users?q={q}` | `POST /search/users?q={q}` | 5m | ✅ |
| GET | `/api/v1/browse?type=&genre=&country=&from=&sort=&page=` | `POST /search/titles?edge=1&types=&genres=&countries=&releases=&sort=` | 10m | ✅ |
| GET | `/api/v1/watchlist` | `GET /sync/mylist/watchlist` | 1m | ✅ |
| GET | `/api/v1/watchlist/{status}` | `GET /sync/mylist/{status}` | 1m | ✅ |
| GET | `/api/v1/me` | `GET /users/me` | 1m | ✅ |
| GET | `/api/v1/calendar` | `POST /calendar/episodes` | 1h | — |
| GET | `/api/v1/articles/featured?page={page}` | `GET /articles/featured` | 10m | — |
| GET | `/api/v1/lists/featured?limit={limit}` | `GET /lists/featured` | 10m | — |
| GET | `/api/v1/lists/popular?limit={limit}` | `GET /lists/popular_voting_lists` | 10m | — |
| GET | `/api/v1/lists/trending?limit={limit}` | `GET /lists/trending` | 10m | — |
| GET | `/api/v1/lists/recent?limit={limit}` | `GET /lists/recent_activity` | 10m | — |
| GET | `/api/v1/articles/{id}` | `GET /articles/{id}` | 1h | — |
| GET | `/api/v1/articles/categories` | `GET /articles/categories` | 24h | — |
| GET | `/api/v1/recommendations?page={page}` | `GET /recommendations` | 10m | — |
| GET | `/api/v1/titles/{id}/feed` | `GET /titles/{id}/feed` | 5m | — |
| GET | `/api/v1/people/{id}/feed` | `GET /people/{id}/feed` | 5m | — |
| GET | `/api/v1/people/{id}/gifts` | `GET /people/{id}/gifts` | 10m | — |
| GET | `/api/v1/groups?page={page}` | `GET /groups` | 1h | — |
| GET | `/api/v1/groups/{id}` | `GET /groups/{id}` | 1h | — |
| GET | `/api/v1/reviews/{id}` | `GET /reviews/{id}` | 1h | — |
| GET | `/api/v1/people/popular?page={page}` | `POST /search/people?limit=50&sort=popular` | 1h | ✅ |
| GET | `/api/v1/people/leaderboard?period={alltime\|weekly\|monthly}` | `GET /people/leaderboard` | 10m | — |
| GET | `/api/v1/people/{id}` | `GET /people/{id}` | 24h | — |
| GET | `/api/v1/people/{id}/credits` | `GET /people/{id}/credits` | 24h | — |
| GET | `/api/v1/users/{id}` | `GET /users/{id}` | 10m | — |
| GET | `/api/v1/users/{id}/stats` | `GET /users/{id}/stats` | 10m | — |
| GET | `/api/v1/users/{id or username}/lists` | `GET /users/{id}/lists` | 10m | ✅ |
| GET | `/api/v1/users/{id or username}/friends` | `GET /users/{id}/friends` | 10m | ✅ |
| GET | `/api/v1/tags/search?q={q}` | `GET /tags/search` | 10m | — |
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
> Expired rows are kept so the next fetch can be compared against them; rows expired for more than 7 days are pruned automatically.

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
| `MDL_LOG_LEVEL` | No | Server log level for `mdlaw:` lines (`DEBUG`, `INFO`, `WARNING`) | `INFO` |
| `MDL_CACHE_MAX_ENTRIES` | No | Max entries in the in-memory cache (LRU eviction) | `10000` |
| `MDL_ADMIN_TOKEN` | No | Enables `POST /api/v1/auth/{login,refresh}` (send as `X-Admin-Token`); unset → those routes 404 | disabled |
| `MDL_RATE_LIMIT` | No | Max requests/minute per client IP on `/api/v1/*` (health exempt); `0` = off | `0` |
| `MDL_TRUST_PROXY` | No | `1` → client IP from `Fly-Client-IP` / `X-Forwarded-For` (only behind your own proxy) | off |

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

Force a re-login / refresh (only when `MDL_ADMIN_TOKEN` is set — otherwise these
routes return `404`). They return the same status object as above; the access
and refresh tokens are never sent to HTTP clients:

```bash
curl -X POST -H "X-Admin-Token: $MDL_ADMIN_TOKEN" http://localhost:8000/api/v1/auth/login
```

> 🔒 **Public instances:** auth-gated routes (`/me`, `/watchlist`, …) act as the
> configured account for every caller. Only expose an instance with
> credentials to people you trust, and consider `MDL_RATE_LIMIT`.

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
| `MDL_ADMIN_TOKEN` | optional (enables the force-login/refresh routes) |
| `MDL_RATE_LIMIT` (+ `MDL_TRUST_PROXY=1` on Fly) | optional (per-IP inbound limit for public instances) |

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
# 94 passed
```

Covers: headers and auth flow (login/refresh/deadlock regressions), upstream
error mapping for both transports, cache (TTL/LRU, SQL change detection,
coalescing), query encoding, route validation and mapping, browse filters, CLI
output/errors/exit codes and logging. All offline — no network.

---

## 📦 Project layout

```
mdlaw/
├── mdlaw.py            # the whole API: config, client, cache (memory/SQL), auth, routes
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
│   ├── conftest.py     # isolation fixture (no real session file, fresh state)
│   ├── test_mdlaw.py   # core offline checks
│   ├── test_hardening.py  # auth / error-handling / cache regression tests
│   └── test_cli.py     # CLI output, errors, exit codes, logging
├── .github/workflows/
│   ├── docker.yml      # GHCR image on version tags
│   └── ci.yml          # ruff + pytest (manual trigger only)
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
