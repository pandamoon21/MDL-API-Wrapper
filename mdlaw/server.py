"""FastAPI server: the ``app``, lifespan and server logging, inbound rate
limit and admin token, OpenAPI response schemas, and every HTTP route.

New endpoint: add the route here, the matching method on ``MDL`` in
library.py, and optionally a CLI command in cli.py."""
from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import caching, client
from ._config import API_BASE, __version__, _qs, log
from .client import _auth

class _UvicornStyleFormatter(logging.Formatter):
    """`INFO:     mdlaw: ...` — lines up with uvicorn's own log lines."""

    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname + ':':<10}mdlaw: {record.getMessage()}"


def _setup_server_logging() -> None:
    """Server log lines in uvicorn's style (``INFO:     mdlaw: ...``). Level
    from MDL_LOG_LEVEL (default INFO). Leaves an already-configured logger
    alone, so a host app's logging config wins."""
    if log.handlers:
        return
    name = os.environ.get("MDL_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        level = logging.INFO
    h = logging.StreamHandler()
    h.setFormatter(_UvicornStyleFormatter())
    log.addHandler(h)
    log.setLevel(level)
    log.propagate = False
    if not isinstance(logging.getLevelName(name), int):
        log.warning("MDL_LOG_LEVEL=%s is not a log level; using INFO", name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup/shutdown: set up logging (client is lazy); close the upstream
    client on shutdown so uvicorn/gunicorn can restart cleanly."""
    _setup_server_logging()
    log.info("transport=%s cache=%s auth=%s", client.TRANSPORT, caching.cache.label,
             "on" if client.auth_enabled() else "off")
    yield
    await client.close_client()


app = FastAPI(
    title="mdlaw — MyDramaList API Wrapper",
    version=__version__,
    description=(
        "Blazing-fast unofficial API for MyDramaList, powered by the official "
        "MDL Android app API.\n\n"
        "**Interactive docs:**\n"
        "- [Swagger UI (playground)](/docs) — try every endpoint live\n"
        "- [ReDoc](/redoc) — clean reference docs\n"
        "- [OpenAPI JSON](/openapi.json) — machine-readable spec\n\n"
        "Not affiliated with MyDramaList."
    ),
    lifespan=lifespan,
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,   # hide schemas section, focus on endpoints
        "tryItOutEnabled": True,          # playground open by default
        "displayRequestDuration": True,   # show latency per request
    },
)

_START = time.time()

# Shared parameter validation: bad input fails fast with 422 instead of
# reaching the upstream (and its throttle). QUERY's max_length also keeps
# cache keys within the MySQL backend's VARCHAR(768) key column.
# (Annotated aliases: a shared Query()/Path() instance gets mutated per route.)
Page = Annotated[int, Query(ge=1, le=1000)]
Limit = Annotated[int, Query(ge=1, le=100)]
SearchQ = Annotated[str, Query(min_length=1, max_length=200)]
PosId = Annotated[int, Path(ge=1)]
UserRef = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$",
                              description="numeric user id or username")]

# Inbound protection (all optional, off by default):
#   MDL_ADMIN_TOKEN  — enables POST /api/v1/auth/{login,refresh}; callers must
#                      send it as `X-Admin-Token`. Unset → those routes 404.
#   MDL_RATE_LIMIT   — max requests per minute per client IP on /api/v1/*
#                      (health excluded). 0/unset → no limit.
#   MDL_TRUST_PROXY  — 1 → take the client IP from Fly-Client-IP /
#                      X-Forwarded-For (only behind a proxy you control).
MDL_ADMIN_TOKEN = os.environ.get("MDL_ADMIN_TOKEN", "").strip()
try:
    RATE_LIMIT = max(0, int(os.environ.get("MDL_RATE_LIMIT", "0").strip() or 0))
except ValueError:
    raise RuntimeError("MDL_RATE_LIMIT must be an integer (requests/minute)") from None
TRUST_PROXY = os.environ.get("MDL_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")
_rate: dict[str, tuple[int, int]] = {}  # ip -> (minute window, count)


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        ip = (request.headers.get("fly-client-ip")
              or request.headers.get("x-forwarded-for", "").split(",")[0].strip())
        if ip:
            return ip
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    path = request.url.path
    if RATE_LIMIT and path.startswith("/api/v1/") and path != "/api/v1/health":
        now = time.time()
        window = int(now // 60)
        ip = _client_ip(request)
        w, n = _rate.get(ip, (window, 0))
        if w != window:
            w, n = window, 0
        if n >= RATE_LIMIT:
            return JSONResponse(
                {"detail": {"error": True, "code": 429, "detail": "rate limit exceeded"}},
                status_code=429, headers={"Retry-After": str(60 - int(now % 60))})
        _rate[ip] = (w, n + 1)
        if len(_rate) > 10_000:  # drop stale windows so the table can't grow unbounded
            for k in [k for k, (kw, _) in _rate.items() if kw != window]:
                del _rate[k]
    return await call_next(request)


def _require_admin(token: str | None) -> None:
    if not MDL_ADMIN_TOKEN:
        raise HTTPException(404, "Not Found")
    if not token or not secrets.compare_digest(token.encode(), MDL_ADMIN_TOKEN.encode()):
        raise HTTPException(403, {"error": True, "code": 403,
                                  "detail": "missing or invalid X-Admin-Token"})


# ---------------------------------------------------------------------------
# OpenAPI response schemas (docs only)
# ---------------------------------------------------------------------------
# Attached with `responses={...}`, never `response_model=`: routes still return
# the upstream JSON untouched. Upstream shapes belong to MDL and are only
# partly known, so every model allows extra fields and every field is optional.
# Fields listed here are the ones the code or tests actually read.
class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ErrorDetail(_Open):
    """Body that every HTTPException raised by mdlaw carries."""
    error: bool | None = None
    code: int | None = None
    detail: Any = None  # message string, or the upstream error JSON as-is


class ErrorResponse(_Open):
    """Error envelope: FastAPI wraps the HTTPException body under `detail`."""
    detail: ErrorDetail | None = None


class NamedRef(_Open):
    """Genre/tag entry inside title detail. Partial; upstream-defined."""
    id: int | None = None
    name: str | None = None
    slug: str | None = None


class TitleDetail(_Open):
    """GET /titles/{id}?expand=1. Partial; upstream-defined, extra fields pass through."""
    id: int | None = None
    title: str | None = None
    year: int | None = None
    episodes: int | None = None
    runtime: int | None = None
    rating: float | None = None
    votes: int | None = None
    ranked: int | None = None
    popularity: int | None = None  # 99999 means not ranked
    release_dates_fmt: str | None = None
    alt_titles: list[str] | None = None
    genres: list[NamedRef] | None = None
    tags: list[NamedRef] | None = None
    permalink: str | None = None
    country: str | None = None
    status: Any = None
    watchers: int | None = None
    certification: str | None = None


class TitleSummary(_Open):
    """One item of POST /search/titles (search and browse). Partial; upstream-defined.
    Search items carry no `genres` field."""
    id: int | None = None
    title: str | None = None
    year: int | None = None
    type: str | None = None
    media_type: str | None = None
    country: str | None = None
    language: str | None = None
    rating: float | None = None


class PersonDetail(_Open):
    """GET /people/{id}. Partial; upstream-defined, extra fields pass through."""
    id: int | None = None
    name: str | None = None
    original_name: str | None = None
    biography: str | None = None
    birthday: str | None = None
    dod: str | None = None  # "0000-00-00" when alive
    nationality: str | None = None
    gender: str | None = None
    flowers: int | None = None
    ranked: int | None = None
    permalink: str | None = None
    slug: str | None = None


class Health(_Open):
    """GET /api/v1/health (built by mdlaw)."""
    status: str | None = None
    service: str | None = None
    auth: bool | None = None


class CacheStats(_Open):
    """Cache backend stats (built by mdlaw)."""
    backend: str | None = None
    hits: int | None = None
    misses: int | None = None
    hit_rate: float | None = None
    entries: int | None = None


class AuthStatus(_Open):
    """GET /api/v1/auth/status (built by mdlaw). Never contains tokens."""
    configured: bool | None = None
    logged_in: bool | None = None
    user: Any = None
    expires_at: float | None = None
    last_login: float | None = None
    last_refresh: float | None = None
    refreshes: int | None = None
    login_error: str | None = None


class DashboardUpstream(_Open):
    base: str | None = None
    key_source: str | None = None
    transport: str | None = None


class DashboardAuth(_Open):
    configured: bool | None = None
    logged_in: bool | None = None
    refreshes: int | None = None


class DashboardRoute(_Open):
    path: str | None = None
    methods: list[str] | None = None


class Dashboard(_Open):
    """GET /api/v1/dashboard and /api/v1/status (built by mdlaw)."""
    status: str | None = None
    service: str | None = None
    version: str | None = None
    uptime_seconds: int | None = None
    cache: CacheStats | None = None
    upstream: DashboardUpstream | None = None
    auth: DashboardAuth | None = None
    routes: list[DashboardRoute] | None = None


# Every mdlaw error (bad input, auth, upstream status, network) uses ErrorResponse.
_ERRORS: dict = {"4XX": {"model": ErrorResponse, "description": "Client or upstream error"},
                 "5XX": {"model": ErrorResponse, "description": "Upstream unreachable or failed"}}


def _doc(model: object) -> dict:
    """`responses=` for a route: documented 200 body plus the shared error body."""
    return {200: {"model": model}, **_ERRORS}


@app.get("/", include_in_schema=False)
async def root():
    """Landing: point humans at the interactive docs."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


def _resp(data: object, ttl: float) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={int(ttl)}"})


@app.get("/api/v1/auth/status", responses=_doc(AuthStatus))
async def auth_status() -> dict:
    """Auth state: configured? logged in? token expiry, last refresh.
    Never includes the access/refresh tokens."""
    return {
        "configured": client.auth_enabled(),
        "logged_in": bool(_auth["token"]),
        "user": _auth["user"],
        "expires_at": _auth["expires_at"],
        "last_login": _auth["last_login"],
        "last_refresh": _auth["last_refresh"],
        "refreshes": _auth["refreshes"],
        "login_error": _auth["login_error"],
    }


@app.post("/api/v1/auth/login", include_in_schema=False)
async def auth_login(x_admin_token: str | None = Header(None)) -> dict:
    """Force a fresh login (credentials from env). Admin-only (MDL_ADMIN_TOKEN);
    returns auth status, never the tokens themselves."""
    _require_admin(x_admin_token)
    await client.login(force=True)
    return await auth_status()


@app.post("/api/v1/auth/refresh", include_in_schema=False)
async def auth_refresh(x_admin_token: str | None = Header(None)) -> dict:
    """Force a token refresh (or re-login if refresh fails). Admin-only."""
    _require_admin(x_admin_token)
    await client.refresh()
    return await auth_status()


@app.get("/api/v1/cache/stats", responses=_doc(CacheStats))
async def cache_stats() -> dict:
    """Cache backend info + hit stats."""
    return await caching._cache_call(caching.cache.stats)


@app.get("/api/v1/health", responses={200: {"model": Health}})  # rate-limit exempt, never errors
async def health() -> dict:
    return {
        "status": "ok",
        "service": "mdlaw",
        "auth": client.auth_enabled(),
    }


@app.get("/api/v1/dashboard", responses=_doc(Dashboard))
async def dashboard() -> dict:
    """Health dashboard: uptime, cache stats, per-route registry."""
    return {
        "status": "ok",
        "service": "mdlaw",
        "version": __version__,
        "uptime_seconds": int(time.time() - _START),
        "cache": await caching._cache_call(caching.cache.stats),
        "upstream": {
            "base": API_BASE,
            "key_source": "env" if os.environ.get("MDL_API_KEY") else "generated-nonce",
            "transport": client.TRANSPORT,
        },
        "auth": {
            "configured": client.auth_enabled(),
            "logged_in": bool(_auth["token"]),
            "refreshes": _auth["refreshes"],
        },
        "routes": [
            {
                "path": r.path,
                "methods": sorted(r.methods or []),
            }
            for r in app.routes
            if getattr(r, "path", "").startswith("/api/v1")
        ],
    }


@app.get("/api/v1/status", responses=_doc(Dashboard))
async def status() -> JSONResponse:
    """Alias of /dashboard kept for monitoring probes."""
    return JSONResponse(await dashboard())


@app.get("/api/v1/genres")
async def genres() -> JSONResponse:
    return _resp(await client.fetch("GET", "/genres", ttl=3600), 3600)


@app.get("/api/v1/languages")
async def languages() -> JSONResponse:
    return _resp(await client.fetch("GET", "/languages/supported?v=2", ttl=3600), 3600)


@app.get("/api/v1/titles/trending")
async def titles_trending(page: Page = 1) -> JSONResponse:
    """Trending titles (requires auth)."""
    return _resp(await client.fetch("GET", f"/titles/trending?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/top_airing")
async def titles_top_airing(page: Page = 1) -> JSONResponse:
    """Top airing titles (requires auth)."""
    return _resp(await client.fetch("GET", f"/titles/top_airing?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/upcoming")
async def titles_upcoming(page: Page = 1) -> JSONResponse:
    """Upcoming titles (requires auth)."""
    return _resp(await client.fetch("GET", f"/titles/upcoming?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/currently_watching")
async def titles_currently_watching(page: Page = 1) -> JSONResponse:
    """Titles the community is currently watching (requires auth)."""
    return _resp(await client.fetch("GET", f"/titles/currently_watching?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/top_movies")
async def titles_top_movies(page: Page = 1) -> JSONResponse:
    """Top movies (requires auth)."""
    return _resp(await client.fetch("GET", f"/titles/top_movies?page={page}", ttl=600, auth=True), 600)


@app.get("/api/v1/titles/{tid}/reviews")
async def title_reviews(tid: PosId) -> JSONResponse:
    return _resp(await client.fetch("GET", f"/titles/{tid}/reviews", ttl=300), 300)


@app.get("/api/v1/titles/{tid}", responses=_doc(TitleDetail))
async def title_detail(tid: PosId) -> JSONResponse:
    """Title detail — requires auth (upstream 400s without a token)."""
    return _resp(await client.fetch("GET", f"/titles/{tid}?expand=1", ttl=300, auth=True), 300)


@app.get("/api/v1/titles/{tid}/recommendations")
async def title_recommendations(tid: PosId) -> JSONResponse:
    return _resp(await client.fetch("GET", f"/titles/{tid}/recommendations", ttl=600, auth=True), 600)


@app.post("/api/v1/search", responses=_doc(list[TitleSummary]))
async def search(q: SearchQ, page: Page = 1) -> JSONResponse:
    """Search titles by keyword (POST /search/titles?edge=1&q=...&page=...)."""
    if not q.strip():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "query param q is required"})
    return _resp(await client.fetch(
        "POST", _qs("/search/titles", edge=1, q=q, page=page, synopsis=1),
        ttl=300, auth=True), 300)


@app.post("/api/v1/search/people")
async def search_people(q: SearchQ, page: Page = 1) -> JSONResponse:
    """Search people by name (POST /search/people?q=...&page=...)."""
    if not q.strip():
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "query param q is required"})
    return _resp(await client.fetch(
        "POST", _qs("/search/people", q=q, page=page),
        ttl=300, auth=True), 300)


@app.get("/api/v1/browse", responses=_doc(list[TitleSummary]))
async def browse(
    kind: Annotated[list[str] | None, Query(alias="type", description="drama | movie | tv-show (or id); repeatable")] = None,
    genre: Annotated[list[str] | None, Query(description="genre name or id; repeatable")] = None,
    country: Annotated[list[str] | None, Query(description="japan | china | south-korea | hong-kong | taiwan | thailand (or id)")] = None,
    released_from: Annotated[int | None, Query(alias="from", ge=1900, le=2100, description="released in this year or later")] = None,
    sort: Annotated[str, Query(pattern="^(top|popular)$")] = "top",
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Page = 1,
) -> JSONResponse:
    """Browse the catalog with server-side filters (requires auth).
    Upstream: POST /search/titles?edge=1&types=&genres=&countries=&releases=&sort="""
    path = await client.browse_path(types=kind, genres=genre, countries=country,
                             released_from=released_from, sort=sort, page=page, q=q)
    return _resp(await client.fetch("POST", path, ttl=600, auth=True), 600)


@app.post("/api/v1/search/users")
async def search_users(q: SearchQ) -> JSONResponse:
    """Search MDL users by name (requires auth)."""
    return _resp(await client.fetch("POST", _qs("/search/users", q=q), ttl=300, auth=True), 300)


@app.get("/api/v1/watchlist")
async def watchlist() -> JSONResponse:
    """Current user's watchlist (requires auth)."""
    return _resp(await client.fetch("GET", "/sync/mylist/watchlist", ttl=60, auth=True), 60)


@app.get("/api/v1/watchlist/{status}")
async def watchlist_by_status(status: str) -> JSONResponse:
    """Watchlist filtered by status: completed|dropped|onhold|plantowatch|notinterested|undecided."""
    valid = ("completed", "dropped", "onhold", "plantowatch", "notinterested", "undecided")
    if status not in valid:
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": f"status must be one of {valid}"})
    return _resp(await client.fetch("GET", f"/sync/mylist/{status}", ttl=60, auth=True), 60)


@app.get("/api/v1/me")
async def me() -> JSONResponse:
    """Current account profile (requires auth)."""
    return _resp(await client.fetch("GET", "/users/me", ttl=60, auth=True), 60)


@app.get("/api/v1/titles/{tid}/comments")
async def title_comments(tid: PosId) -> JSONResponse:
    return _resp(await client.fetch("GET", f"/titles/{tid}/comments", ttl=300), 300)


@app.get("/api/v1/titles/{tid}/credits")
async def title_credits(tid: PosId) -> JSONResponse:
    # Auth-gated upstream (400 without token); same clear error as other
    # auth-gated endpoints.
    return _resp(await client.fetch("GET", f"/titles/{tid}/credits", ttl=300, auth=True), 300)


@app.get("/api/v1/calendar")
async def calendar() -> JSONResponse:
    return _resp(await client.fetch("POST", "/calendar/episodes", body={}, ttl=3600), 3600)


@app.get("/api/v1/articles/featured")
async def articles_featured(page: Page = 1) -> JSONResponse:
    return _resp(await client.fetch("GET", f"/articles/featured?page={page}", ttl=600), 600)


@app.get("/api/v1/lists/featured")
async def lists_featured(limit: Limit = 5) -> JSONResponse:
    return _resp(await client.fetch("GET", f"/lists/featured?limit={limit}", ttl=600), 600)


@app.get("/api/v1/lists/popular")
async def lists_popular(limit: Limit = 5) -> JSONResponse:
    return _resp(await client.fetch("GET", f"/lists/popular_voting_lists?limit={limit}", ttl=600), 600)


@app.get("/api/v1/lists/trending")
async def lists_trending(limit: Limit = 5) -> JSONResponse:
    """Trending user lists."""
    return _resp(await client.fetch("GET", f"/lists/trending?limit={limit}", ttl=600), 600)


@app.get("/api/v1/lists/recent")
async def lists_recent(limit: Limit = 5) -> JSONResponse:
    """Lists with recent activity."""
    return _resp(await client.fetch("GET", f"/lists/recent_activity?limit={limit}", ttl=600), 600)


@app.get("/api/v1/people/popular")
async def people_popular(page: Page = 1) -> JSONResponse:
    """Most popular people, 50 per page (requires auth)."""
    return _resp(await client.fetch("POST", _qs("/search/people", page=page, limit=50, sort="popular"),
                             ttl=3600, auth=True), 3600)


@app.get("/api/v1/people/leaderboard")
async def leaderboard(period: str = "alltime") -> JSONResponse:
    if period not in ("alltime", "weekly", "monthly"):
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "period must be alltime|weekly|monthly"})
    return _resp(await client.fetch("GET", f"/people/leaderboard?time_period={period}", ttl=600), 600)


@app.get("/api/v1/people/{pid}", responses=_doc(PersonDetail))
async def people(pid: PosId) -> JSONResponse:
    return _resp(await client.fetch("GET", f"/people/{pid}", ttl=86400), 86400)


@app.get("/api/v1/people/{pid}/credits")
async def people_credits(pid: PosId) -> JSONResponse:
    """Filmography (cast/crew) for a person/actor."""
    return _resp(await client.fetch("GET", f"/people/{pid}/credits", ttl=86400), 86400)


@app.get("/api/v1/users/{uid}")
async def user(uid: PosId) -> JSONResponse:
    """Public user profile by id."""
    return _resp(await client.fetch("GET", f"/users/{uid}", ttl=600), 600)


@app.get("/api/v1/users/{uid}/stats")
async def user_stats(uid: PosId) -> JSONResponse:
    """Public user stats (collected/rated counts, distribution)."""
    return _resp(await client.fetch("GET", f"/users/{uid}/stats", ttl=600), 600)


@app.get("/api/v1/users/{user}/lists")
async def user_lists(user: UserRef) -> JSONResponse:
    """A user's custom lists (requires auth). ``user`` = id or username."""
    return _resp(await client.fetch("GET", f"/users/{user}/lists", ttl=600, auth=True), 600)


@app.get("/api/v1/users/{user}/friends")
async def user_friends(user: UserRef) -> JSONResponse:
    """A user's friends (requires auth). ``user`` = id or username."""
    return _resp(await client.fetch("GET", f"/users/{user}/friends", ttl=600, auth=True), 600)


@app.get("/api/v1/recommendations")
async def recommendations(page: Page = 1) -> JSONResponse:
    """Community recommendations: "if you liked X, watch Y", with the reason."""
    return _resp(await client.fetch("GET", f"/recommendations?page={page}", ttl=600), 600)


@app.get("/api/v1/titles/{tid}/feed")
async def title_feed(tid: PosId) -> JSONResponse:
    """Community activity about a title (fetched anonymously)."""
    return _resp(await client.fetch("GET", f"/titles/{tid}/feed", ttl=300), 300)


@app.get("/api/v1/people/{pid}/feed")
async def people_feed(pid: PosId) -> JSONResponse:
    """Community activity about a person (fetched anonymously)."""
    return _resp(await client.fetch("GET", f"/people/{pid}/feed", ttl=300), 300)


@app.get("/api/v1/people/{pid}/gifts")
async def people_gifts(pid: PosId) -> JSONResponse:
    """Flowers sent to a person: top and most recent givers."""
    return _resp(await client.fetch("GET", f"/people/{pid}/gifts", ttl=600), 600)


@app.get("/api/v1/articles/categories")
async def articles_categories() -> JSONResponse:
    """Article categories (news, interviews, …)."""
    return _resp(await client.fetch("GET", "/articles/categories", ttl=86400), 86400)


@app.get("/api/v1/groups")
async def groups(page: Page = 1) -> JSONResponse:
    """Community groups, 20 per page."""
    return _resp(await client.fetch("GET", f"/groups?page={page}", ttl=3600), 3600)


@app.get("/api/v1/groups/{gid}")
async def group(gid: PosId) -> JSONResponse:
    """A community group."""
    return _resp(await client.fetch("GET", f"/groups/{gid}", ttl=3600), 3600)


@app.get("/api/v1/reviews/{rid}")
async def review(rid: PosId) -> JSONResponse:
    """A single review: ratings breakdown, text, author, title."""
    return _resp(await client.fetch("GET", f"/reviews/{rid}", ttl=3600), 3600)


@app.get("/api/v1/articles/{aid}")
async def article(aid: PosId) -> JSONResponse:
    """A single article (news/feature) with its body text."""
    return _resp(await client.fetch("GET", f"/articles/{aid}", ttl=3600), 3600)


@app.get("/api/v1/tags/search")
async def search_tags(q: SearchQ) -> JSONResponse:
    """Search drama tags (e.g. q=romance → Romance, Bromance, …)."""
    return _resp(await client.fetch("GET", _qs("/tags/search", q=q), ttl=600), 600)


# Payment routes are outside a drama-data wrapper's scope: hidden from the
# OpenAPI schema/docs, but still served so existing callers keep working.
@app.get("/api/v1/payment/plans", include_in_schema=False)
async def payment_plans() -> JSONResponse:
    return _resp(await client.fetch("GET", "/payment/plans", ttl=3600), 3600)


@app.get("/api/v1/payment/coins", include_in_schema=False)
async def payment_coins() -> JSONResponse:
    return _resp(await client.fetch("GET", "/payment/coins", ttl=3600), 3600)
