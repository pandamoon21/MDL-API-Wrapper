# mdlaw

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

The Android app talks to a JSON API behind Cloudflare. `mdlaw` replicates the exact request headers the app sends (validated: without them the API returns **403**), so it gets **real structured data** — not scraped HTML.

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
export MDL_API_KEY=<your-key>        # REQUIRED — no key embedded in the public build
mdlaw                                # serves http://0.0.0.0:8000 (Swagger at /docs)
mdlaw self                           # offline self-check (needs MDL_API_KEY)
```

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
      - MDL_API_KEY=${MDL_API_KEY}   # optional
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
| POST | `/api/v1/search?q={q}` | `POST /search` (body `{"q", "synopsis"}`) | 5m | ✅ |
| POST | `/api/v1/search/people?q={q}` | `POST /search/people` (body `{"q"}`) | 5m | ✅ |
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

- **Async + connection pooling** — one `httpx.AsyncClient` reuses TCP/TLS connections.
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
| `MDL_API_KEY` | **Yes** | The MDL app API key (not in source — you provide it) | — |
| `MDL_USERNAME` | No | Enables auth-gated endpoints (title detail, search, watchlist, `/me`) | disabled |
| `MDL_PASSWORD` | No | Password for the account above (paired with `MDL_USERNAME`) | disabled |
| `MDL_CACHE_BACKEND` | No | `memory` · `sqlite` · `mysql` · `postgres` | `memory` |
| `MDL_CACHE_DB_URL` | Only if backend ≠ memory | DSN, e.g. `sqlite:///mdlaw_cache.db` | — |

### 🔐 API key (required)

The MDL Android app talks to `app-api.mydramalist.com` with a hardcoded
`mdl-api-key` header. This public build does **not** embed that key — you must
provide it yourself (extract from the APK, or get it from a collaborator
who has access to the app key). The server **refuses to start** without `MDL_API_KEY`:

```bash
export MDL_API_KEY=<your-key>
uvicorn mdlaw:app --port 8000
```

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

All options below. This is the **public** build: `MDL_API_KEY` is **required**
everywhere — the app refuses to start without it.

| Env | Public build |
|---|---|
| `MDL_API_KEY` | **required** |
| `MDL_USERNAME` + `MDL_PASSWORD` | optional (auth-gated endpoints) |
| `MDL_CACHE_BACKEND` + `MDL_CACHE_DB_URL` | optional (persistent cache) |

### Option A — Fly.io (recommended)

```bash
# 1. One-time: create the app (reads fly.toml)
fly launch --no-deploy

# 2. Set secrets (MDL_API_KEY REQUIRED)
fly secrets set MDL_API_KEY=<your-key>
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
#   Settings → Environment Variables → add MDL_API_KEY (REQUIRED),
#   MDL_USERNAME, MDL_PASSWORD, MDL_CACHE_BACKEND, MDL_CACHE_DB_URL
```

Two files are already included (`api/index.py` + `vercel.json`). Caveats:

- **Cold starts**: Python serverless functions boot per request — first hit after idle is slow (~2–5 s).
- **Cache is per-instance & ephemeral**: the TTL cache resets between cold starts, so MDL's WAF sees more upstream traffic than on a persistent host. Still throttled (2 concurrent, 0.5 s interval), so it holds up, but it's not "blazing" on first hits.
- **SQL cache not recommended on Vercel**: serverless has no persistent filesystem — use an external MySQL/Postgres if you want persistence.
- Fine for a demo / low-traffic personal API. For consistent speed, use **Fly.io** (persistent instance, warm cache).

### Option C — Docker

```bash
docker build -t mdlaw .

# MDL_API_KEY REQUIRED
docker run -p 8000:8000 \
  -e MDL_API_KEY=<your-key> \
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
      MDL_API_KEY: ${MDL_API_KEY}        # required
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
MDL_API_KEY=your-key
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
├── requirements.txt    # fastapi, uvicorn[standard], httpx
├── Dockerfile          # python:3.12-slim, non-root
├── docker-compose.yml  # one-command production deploy (with healthcheck)
├── fly.toml            # Fly.io config (port 8000, region sin, always-on)
├── pytest.ini          # pythonpath for tests
├── .env.example        # API key + account + cache backend placeholders
├── tests/
│   └── test_mdlaw.py   # 8 offline checks
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
