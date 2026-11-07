"""Python library layer: ``MDL``, an async client over fetch() that shares
the process-wide cache, throttle, auth and transport (no server needed)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import HTTPException

from . import caching, client
from ._config import _qs, log
from .client import _auth


def _seg(value: object) -> str:
    """One URL path segment from a caller-supplied id or username, escaped so
    input like ``"../titles/1"`` can't change which upstream route is hit."""
    text = str(value).strip()
    if text in ("", ".", ".."):
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": f"invalid id or username: {value!r}"})
    return quote(text, safe="")


def _cap(items: list, limit: int | None) -> list:
    """``limit`` of None or <= 0 means no cap (same rule for every method)."""
    return items[:limit] if limit and limit > 0 else items

# ---------------------------------------------------------------------------
# Python package layer — use mdlaw as a library without the HTTP server.
#   from mdlaw import MDL
#   mdl = MDL()
#   genres = await mdl.genres()
#   title = await mdl.title(686)
# Each method wraps client.fetch() → shared cache, throttle, auth, transport.
# ---------------------------------------------------------------------------
class MDL:
    """Pythonic client for the MDL app API (no server needed).

    All methods are async and share the module-level cache, throttle, auth
    and transport. Construct once, call many, `await mdl.close()` at exit.
    """

    def __init__(self, transport: str | None = None,
                 username: str | None = None, password: str | None = None):
        if transport:
            name = transport.strip().lower()
            if name not in ("curl_cffi", "httpx"):
                raise ValueError("transport must be 'curl_cffi' or 'httpx'")
            client.TRANSPORT = name
        if username is not None or password is not None:
            client.MDL_USERNAME = (username or "").strip()
            client.MDL_PASSWORD = (password or "").strip()
        # No env credentials → reuse a session saved by `mdlaw auth`.
        if not client.auth_enabled() and not _auth["token"]:
            client._load_auth()

    async def get(self, path: str, ttl: float = 3600, auth: bool = False) -> object:
        return await client.fetch("GET", path, ttl=ttl, auth=auth)

    async def post(self, path: str, body: dict, ttl: float = 300,
                   auth: bool = False) -> object:
        return await client.fetch("POST", path, ttl=ttl, auth=auth, body=body)

    # --- public data ---
    async def genres(self) -> object:
        return await self.get("/genres", ttl=3600)

    async def languages(self) -> object:
        return await self.get("/languages/supported?v=2", ttl=3600)

    async def calendar(self) -> object:
        return await self.post("/calendar/episodes", {}, ttl=3600)

    async def articles_featured(self, page: int = 1) -> object:
        return await self.get(f"/articles/featured?page={page}", ttl=600)

    async def lists_featured(self, limit: int = 5) -> object:
        return await self.get(f"/lists/featured?limit={limit}", ttl=600)

    async def lists_popular(self, limit: int = 5) -> object:
        return await self.get(f"/lists/popular_voting_lists?limit={limit}", ttl=600)

    async def leaderboard(self, period: str = "alltime") -> object:
        return await self.get(_qs("/people/leaderboard", time_period=period), ttl=600)

    async def people(self, pid: int) -> object:
        return await self.get(f"/people/{_seg(pid)}", ttl=86400)

    async def people_credits(self, pid: int) -> object:
        """Filmography (cast/crew) for a person/actor."""
        return await self.get(f"/people/{_seg(pid)}/credits", ttl=86400)

    async def user(self, uid: int) -> object:
        """Public user profile by id."""
        return await self.get(f"/users/{_seg(uid)}", ttl=600)

    async def user_stats(self, uid: int) -> object:
        """Public user stats (collected/rated counts, distribution)."""
        return await self.get(f"/users/{_seg(uid)}/stats", ttl=600)

    async def search_tags(self, q: str) -> object:
        """Search drama tags (e.g. q=romance → Romance, Bromance, …)."""
        return await self.get(_qs("/tags/search", q=q), ttl=600)

    async def payment_plans(self) -> object:
        return await self.get("/payment/plans", ttl=3600)

    async def payment_coins(self) -> object:
        return await self.get("/payment/coins", ttl=3600)

    # --- titles ---
    async def titles_trending(self, page: int = 1) -> object:
        """Trending titles (requires auth)."""
        return await self.get(f"/titles/trending?page={page}", ttl=600, auth=True)

    async def titles_top_airing(self, page: int = 1) -> object:
        """Top airing titles (requires auth)."""
        return await self.get(f"/titles/top_airing?page={page}", ttl=600, auth=True)

    async def titles_upcoming(self, page: int = 1) -> object:
        """Upcoming titles (requires auth)."""
        return await self.get(f"/titles/upcoming?page={page}", ttl=600, auth=True)

    async def titles_currently_watching(self, page: int = 1) -> object:
        """Titles the community is currently watching (requires auth)."""
        return await self.get(f"/titles/currently_watching?page={page}", ttl=600, auth=True)

    async def titles_top_movies(self, page: int = 1) -> object:
        """Top movies (requires auth)."""
        return await self.get(f"/titles/top_movies?page={page}", ttl=600, auth=True)

    async def title(self, tid: int) -> object:
        """Title detail — requires account credentials (auth-gated upstream)."""
        return await self.get(f"/titles/{_seg(tid)}?expand=1", ttl=300, auth=True)

    async def title_reviews(self, tid: int) -> object:
        return await self.get(f"/titles/{_seg(tid)}/reviews", ttl=300)

    async def title_recommendations(self, tid: int) -> object:
        return await self.get(f"/titles/{_seg(tid)}/recommendations", ttl=600, auth=True)

    async def title_comments(self, tid: int) -> object:
        return await self.get(f"/titles/{_seg(tid)}/comments", ttl=300)

    async def title_credits(self, tid: int) -> object:
        return await self.get(f"/titles/{_seg(tid)}/credits", ttl=300, auth=True)

    # --- search (POST) ---
    # Upstream search is POST /search/titles?edge=1&q=<q>&page=<page>&synopsis=1.
    # `q` and `page` are URL query params, NOT a JSON body. Search items carry
    # country/language/type/media_type/year but no genres field; post-filters
    # below run in Python. Genre filtering: use browse_by_genre().
    async def search(self, q: str = "", country: str | None = None,
                     language: str | None = None, type: str | None = None,
                     media_type: str | None = None, year: int | None = None,
                     limit: int | None = None, page: int = 1) -> object:
        """Search titles (POST /search/titles?edge=1&q=...&page=...).

        ``q`` is passed upstream as a URL query param and IS honored (verified
        live: ``q="crash landing on you"`` → Crash Landing on You; ``page=2``
        returns different results). ``country``/``language``/``type``/
        ``media_type``/``year``/``limit`` are client-side post-filters over the
        fields search results carry. Genre is NOT filterable here (search items
        have no genres field) — use :meth:`browse_by_genre` instead.
        """
        results = await self.post(_qs("/search/titles", edge=1, q=q, page=page, synopsis=1),
                                  {}, ttl=300, auth=True)
        if not isinstance(results, list):
            return results
        filtered = results
        if country:
            filtered = [it for it in filtered
                        if (it.get("country") or "").lower() == country.lower()]
        if language:
            filtered = [it for it in filtered
                        if (it.get("language") or "").lower() == language.lower()]
        if type:
            filtered = [it for it in filtered
                        if (it.get("type") or "").lower() == type.lower()]
        if media_type:
            filtered = [it for it in filtered
                        if (it.get("media_type") or "").lower() == media_type.lower()]
        if year:
            filtered = [it for it in filtered if it.get("year") == int(year)]
        return _cap(filtered, limit)

    async def browse(self, types: object = None, genres: object = None,
                     countries: object = None, released_from: int | None = None,
                     sort: str = "top", page: int = 1, q: str | None = None) -> object:
        """Browse the catalog with **server-side** filters (requires auth).

        ``types``: ``"drama"``, ``"movie"``, ``"tv-show"`` (or ids 68/77/86);
        ``genres``: names, slugs or ids from :meth:`genres`; ``countries``:
        ``"japan"``, ``"china"``, ``"south-korea"``, ``"hong-kong"``,
        ``"taiwan"``, ``"thailand"`` (or ids 1–6). Each accepts a single value
        or a list. ``released_from``: year (inclusive). ``sort``: ``"top"``
        (by rating) or ``"popular"``. 20 results per page.

            await mdl.browse(types="drama", genres="romance",
                             countries="south-korea", sort="popular")
        """
        path = await client.browse_path(types=types, genres=genres, countries=countries,
                                 released_from=released_from, sort=sort, page=page, q=q)
        return await self.post(path, {}, ttl=600, auth=True)

    async def browse_by_genre(self, genre_id: int, limit: int | None = 10,
                              source: str = "search") -> list:
        """Titles in ``genre_id``.

        ``source="search"`` (default) uses the server-side genre filter — one
        request, returns search items (no ``genres`` field). ``"trending"`` /
        ``"top_movies"`` keep the old behaviour: fetch each candidate's detail
        (one auth-gated call per title) and return the matching details.
        ``limit`` of None or <= 0 means no cap, as in :meth:`search`.
        """
        if source == "search":
            items = await self.browse(genres=genre_id)
            return _cap(items, limit) if isinstance(items, list) else []
        pool_size = max(limit * 2, 10) if limit and limit > 0 else 10
        if source == "trending":
            pool = await self.get(f"/titles/trending?limit={pool_size}",
                                  ttl=300, auth=True)
        elif source == "top_movies":
            pool = await self.get(f"/titles/top_movies?limit={pool_size}",
                                  ttl=300, auth=True)
        else:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": "source must be 'search', 'trending' or 'top_movies'"})
        candidates = pool if isinstance(pool, list) else (pool.get("items", []) if isinstance(pool, dict) else [])
        out: list = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            tid = item.get("id") or item.get("rid")
            if not tid:
                continue
            try:
                detail = await self.title(tid)
            except HTTPException as e:
                # Missing credentials (400), a rejected session (401/428) or a
                # Cloudflare block (403) fails every remaining title too —
                # surface it instead of returning a silently empty list.
                if e.status_code in (400, 401, 403, 428):
                    raise
                log.warning("genre_titles: skipping title %s (HTTP %s)", tid, e.status_code)
                continue
            genres = detail.get("genres") or [] if isinstance(detail, dict) else []
            gids = [g.get("id") for g in genres if isinstance(g, dict)]
            if genre_id in gids:
                out.append(detail)
                if limit and 0 < limit <= len(out):
                    break
        return out

    async def people_popular(self, page: int = 1) -> object:
        """Most popular people, 50 per page (requires auth)."""
        return await self.post(_qs("/search/people", page=page, limit=50, sort="popular"), {},
                               ttl=3600, auth=True)

    async def search_users(self, q: str) -> object:
        """Search MDL users by name (requires auth)."""
        return await self.post(_qs("/search/users", q=q), {}, ttl=300, auth=True)

    async def lists_trending(self, limit: int = 5) -> object:
        return await self.get(f"/lists/trending?limit={limit}", ttl=600)

    async def lists_recent(self, limit: int = 5) -> object:
        """Lists with recent activity."""
        return await self.get(f"/lists/recent_activity?limit={limit}", ttl=600)

    async def user_lists(self, user: int | str) -> object:
        """A user's custom lists (id or username; requires auth)."""
        return await self.get(f"/users/{_seg(user)}/lists", ttl=600, auth=True)

    async def user_friends(self, user: int | str) -> object:
        """A user's friends (id or username; requires auth)."""
        return await self.get(f"/users/{_seg(user)}/friends", ttl=600, auth=True)

    async def recommendations(self, page: int = 1) -> object:
        """Community recommendations: "if you liked X, watch Y", with the reason."""
        return await self.get(f"/recommendations?page={page}", ttl=600)

    async def title_feed(self, tid: int) -> object:
        """Community activity about a title."""
        return await self.get(f"/titles/{_seg(tid)}/feed", ttl=300)

    async def people_feed(self, pid: int) -> object:
        """Community activity about a person."""
        return await self.get(f"/people/{_seg(pid)}/feed", ttl=300)

    async def people_gifts(self, pid: int) -> object:
        """Flowers sent to a person: ``top`` and ``recents`` givers."""
        return await self.get(f"/people/{_seg(pid)}/gifts", ttl=600)

    async def articles_categories(self) -> object:
        return await self.get("/articles/categories", ttl=86400)

    async def groups(self, page: int = 1) -> object:
        """Community groups, 20 per page (``{"items": [...]}``)."""
        return await self.get(f"/groups?page={page}", ttl=3600)

    async def group(self, gid: int) -> object:
        return await self.get(f"/groups/{_seg(gid)}", ttl=3600)

    async def review(self, rid: int) -> object:
        """A single review: ratings breakdown, text, author, title."""
        return await self.get(f"/reviews/{_seg(rid)}", ttl=3600)

    async def article(self, aid: int) -> object:
        """A single article with its body text."""
        return await self.get(f"/articles/{_seg(aid)}", ttl=3600)

    async def search_people(self, q: str, page: int = 1) -> object:
        return await self.post(_qs("/search/people", q=q, page=page), {},
                               ttl=300, auth=True)

    # --- account (requires credentials) ---
    _WATCH_STATUSES = ("completed", "dropped", "onhold", "plantowatch",
                       "notinterested", "undecided")

    async def watchlist(self, status: str | None = None) -> object:
        if status and status not in self._WATCH_STATUSES:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": f"status must be one of {self._WATCH_STATUSES}"})
        path = f"/sync/mylist/{status}" if status else "/sync/mylist/watchlist"
        return await self.get(path, ttl=60, auth=True)

    async def me(self) -> object:
        return await self.get("/users/me", ttl=60, auth=True)

    # --- lifecycle ---
    async def close(self) -> None:
        await client.close_client()

    def stats(self) -> dict:
        return caching.cache.stats()
