"""Offline contract tests for the ``MDL`` library layer (mdlaw/library.py).

Each wrapper is checked against the upstream request it sends (method, path
with query string, cache TTL, auth flag, body) by stubbing ``client.fetch``,
the same seam the other library tests use. No network, no real waiting."""
import asyncio
import logging

import pytest
from fastapi import HTTPException

import mdlaw


@pytest.fixture
def upstream(monkeypatch):
    """Stub client.fetch. ``upstream.calls`` records requests; ``upstream.reply``
    maps a path (or a callable) to the response; the default reply is {}."""
    class Upstream:
        calls: list = []
        reply = None

    up = Upstream()
    up.calls = []

    async def fake_fetch(method, path, ttl=3600, auth=False, body=None):
        up.calls.append((method, path, ttl, auth, body))
        r = up.reply
        r = r(method, path) if callable(r) else r
        if isinstance(r, BaseException):
            raise r
        return {} if r is None else r
    monkeypatch.setattr(mdlaw.client, "fetch", fake_fetch)
    return up


def call(name, *args, **kwargs):
    async def go():
        return await getattr(mdlaw.MDL(), name)(*args, **kwargs)
    return asyncio.run(go())


G, P = "GET", "POST"

# (method name, args, kwargs) -> (http method, upstream path, ttl, auth, body)
WRAPPERS = [
    ("genres", (), {}, (G, "/genres", 3600, False, None)),
    ("languages", (), {}, (G, "/languages/supported?v=2", 3600, False, None)),
    ("calendar", (), {}, (P, "/calendar/episodes", 3600, False, {})),
    ("articles_featured", (3,), {}, (G, "/articles/featured?page=3", 600, False, None)),
    ("lists_featured", (), {}, (G, "/lists/featured?limit=5", 600, False, None)),
    ("lists_popular", (7,), {}, (G, "/lists/popular_voting_lists?limit=7", 600, False, None)),
    ("leaderboard", (), {}, (G, "/people/leaderboard?time_period=alltime", 600, False, None)),
    ("leaderboard", ("monthly",), {}, (G, "/people/leaderboard?time_period=monthly", 600, False, None)),
    ("people", (42,), {}, (G, "/people/42", 86400, False, None)),
    ("search_tags", ("a&page=9",), {}, (G, "/tags/search?q=a%26page%3D9", 600, False, None)),
    ("payment_plans", (), {}, (G, "/payment/plans", 3600, False, None)),
    ("payment_coins", (), {}, (G, "/payment/coins", 3600, False, None)),
    ("titles_top_airing", (2,), {}, (G, "/titles/top_airing?page=2", 600, True, None)),
    ("titles_upcoming", (), {}, (G, "/titles/upcoming?page=1", 600, True, None)),
    ("titles_currently_watching", (4,), {},
     (G, "/titles/currently_watching?page=4", 600, True, None)),
    ("title_reviews", (9,), {}, (G, "/titles/9/reviews", 300, False, None)),
    ("title_recommendations", (9,), {}, (G, "/titles/9/recommendations", 600, True, None)),
    ("title_comments", (9,), {}, (G, "/titles/9/comments", 300, False, None)),
    ("title_credits", (9,), {}, (G, "/titles/9/credits", 300, True, None)),
    ("people_popular", (2,), {},
     (P, "/search/people?page=2&limit=50&sort=popular", 3600, True, {})),
    ("search_users", ("jo doe",), {}, (P, "/search/users?q=jo+doe", 300, True, {})),
    ("lists_trending", (), {}, (G, "/lists/trending?limit=5", 600, False, None)),
    ("lists_recent", (8,), {}, (G, "/lists/recent_activity?limit=8", 600, False, None)),
    ("user_lists", ("alice",), {}, (G, "/users/alice/lists", 600, True, None)),
    ("user_friends", (5,), {}, (G, "/users/5/friends", 600, True, None)),
    ("recommendations", (2,), {}, (G, "/recommendations?page=2", 600, False, None)),
    ("title_feed", (9,), {}, (G, "/titles/9/feed", 300, False, None)),
    ("people_feed", (42,), {}, (G, "/people/42/feed", 300, False, None)),
    ("people_gifts", (42,), {}, (G, "/people/42/gifts", 600, False, None)),
    ("articles_categories", (), {}, (G, "/articles/categories", 86400, False, None)),
    ("groups", (), {}, (G, "/groups?page=1", 3600, False, None)),
    ("group", (11,), {}, (G, "/groups/11", 3600, False, None)),
    ("review", (12,), {}, (G, "/reviews/12", 3600, False, None)),
    ("article", (13,), {}, (G, "/articles/13", 3600, False, None)),
    ("search_people", ("iu",), {"page": 2}, (P, "/search/people?q=iu&page=2", 300, True, {})),
    ("watchlist", ("plantowatch",), {}, (G, "/sync/mylist/plantowatch", 60, True, None)),
]


@pytest.mark.parametrize("name,args,kwargs,expected", WRAPPERS,
                         ids=[f"{w[0]}-{i}" for i, w in enumerate(WRAPPERS)])
def test_wrapper_sends_upstream_request(upstream, name, args, kwargs, expected):
    upstream.reply = {"payload": name}
    assert call(name, *args, **kwargs) == {"payload": name}  # returned as-is
    assert upstream.calls == [expected]


def test_upstream_errors_propagate(upstream):
    upstream.reply = HTTPException(404, {"detail": "nope"})
    with pytest.raises(HTTPException) as ei:
        call("people", 1)
    assert ei.value.status_code == 404


def test_watchlist_rejects_unknown_status_without_request(upstream):
    with pytest.raises(HTTPException) as ei:
        call("watchlist", "watching")
    assert ei.value.status_code == 400 and upstream.calls == []


# --- constructor ---------------------------------------------------------

def test_constructor_sets_transport(monkeypatch):
    monkeypatch.setattr(mdlaw.client, "TRANSPORT", "curl_cffi")
    mdlaw.MDL()
    assert mdlaw.client.TRANSPORT == "curl_cffi"   # None keeps the current one
    mdlaw.MDL(transport="httpx")
    assert mdlaw.client.TRANSPORT == "httpx"


def test_constructor_strips_credentials_and_blanks_missing_one():
    mdlaw.MDL(username="  user@x.com ")
    assert (mdlaw.client.MDL_USERNAME, mdlaw.client.MDL_PASSWORD) == ("user@x.com", "")


def test_constructor_skips_saved_session_when_token_present(monkeypatch):
    loads = []
    monkeypatch.setattr(mdlaw.client, "_load_auth", lambda: loads.append(1) or True)
    mdlaw.client._auth["token"] = "live"
    mdlaw.MDL()
    assert loads == []
    mdlaw.client._auth["token"] = None
    mdlaw.MDL()
    assert loads == [1]


# --- search post-filters -------------------------------------------------

FEED = [
    {"id": 1, "country": "South Korea", "language": "Korean", "type": "Drama",
     "media_type": "Korean Drama", "year": 2024},
    {"id": 2, "country": "Japan", "language": "Japanese", "type": "Movie",
     "media_type": "Japanese Movie", "year": 2023},
    {"id": 3, "country": None, "language": None, "type": None, "media_type": None},
]


@pytest.mark.parametrize("kwargs,ids", [
    ({"language": "KOREAN"}, [1]),
    ({"media_type": "japanese movie"}, [2]),
    ({"year": "2023"}, [2]),            # coerced with int()
    ({"limit": 2}, [1, 2]),
    ({}, [1, 2, 3]),                    # items with null fields survive no-filter
])
def test_search_post_filters(upstream, kwargs, ids):
    upstream.reply = FEED
    assert [it["id"] for it in call("search", "x", **kwargs)] == ids


def test_search_sends_query_and_page_as_url_params(upstream):
    upstream.reply = []
    call("search", "a b&c", page=3)
    assert upstream.calls == [
        (P, "/search/titles?edge=1&q=a+b%26c&page=3&synopsis=1", 300, True, {})]


def test_search_non_list_response_returned_unfiltered(upstream):
    upstream.reply = {"items": [{"id": 1}], "total": 1}
    assert call("search", country="Japan") == {"items": [{"id": 1}], "total": 1}


# --- browse / browse_by_genre --------------------------------------------

def test_browse_builds_filtered_search_path(upstream):
    upstream.reply = lambda m, p: ([{"id": 7, "name": "Romance", "slug": "romance"}]
                                   if p == "/genres" else [{"id": 1}])
    assert call("browse", types="drama", genres="romance", countries="korea",
                released_from=2020, sort="popular", page=2, q="love") == [{"id": 1}]
    assert upstream.calls[-1] == (
        P, "/search/titles?edge=1&q=love&page=2&types=68&genres=7&countries=3"
           "&releases=2020&sort=popular", 600, True, {})


def test_browse_invalid_sort_fails_before_request(upstream):
    with pytest.raises(HTTPException) as ei:
        call("browse", sort="newest")
    assert ei.value.status_code == 400 and upstream.calls == []


@pytest.mark.parametrize("reply,limit,expected", [
    ([{"id": i} for i in range(5)], 2, [{"id": 0}, {"id": 1}]),
    ({"items": [{"id": 1}]}, 10, []),   # non-list browse result -> empty list
])
def test_browse_by_genre_search_source(upstream, reply, limit, expected):
    upstream.reply = reply
    assert call("browse_by_genre", 18, limit=limit) == expected
    assert upstream.calls == [(P, "/search/titles?edge=1&page=1&genres=18&sort=top",
                               600, True, {})]


def test_browse_by_genre_rejects_unknown_source(upstream):
    with pytest.raises(HTTPException) as ei:
        call("browse_by_genre", 1, source="popular")
    assert ei.value.status_code == 400 and upstream.calls == []


def _detail_upstream(upstream, pool, details):
    def reply(method, path):
        if path.startswith("/titles/") and "expand=1" in path:
            d = details[int(path.split("/")[2].split("?")[0])]
            return d
        return pool
    upstream.reply = reply


def _detail_ids(upstream):
    return [int(p.split("/")[2].split("?")[0]) for _, p, *_ in upstream.calls
            if "expand=1" in p]


def test_browse_by_genre_detail_pool_shapes_and_limit(upstream):
    pool = {"items": [{"id": 1}, {"title": "no id"}, {"rid": 2}, {"id": 3}, {"id": 4}]}
    details = {1: {"genres": [{"id": 5}]}, 2: {"genres": None}, 3: {"genres": [{"id": 5}]},
               4: {"genres": [{"id": 5}]}}
    _detail_upstream(upstream, pool, details)
    out = call("browse_by_genre", 5, limit=2, source="trending")
    assert out == [details[1], details[3]]
    # pool size is max(limit*2, 10); id-less items skipped; rid accepted;
    # stops fetching details once ``limit`` matches are found (4 never asked)
    assert upstream.calls[0][:4] == (G, "/titles/trending?limit=10", 300, True)
    assert _detail_ids(upstream) == [1, 2, 3]


def test_browse_by_genre_top_movies_pool_size_scales_with_limit(upstream):
    _detail_upstream(upstream, "unexpected", {})
    assert call("browse_by_genre", 5, limit=8, source="top_movies") == []
    assert upstream.calls == [(G, "/titles/top_movies?limit=16", 300, True, None)]


@pytest.mark.parametrize("status", [404, 500, 502])
def test_browse_by_genre_logs_and_skips_per_title_errors(upstream, caplog, status):
    _detail_upstream(upstream, [{"id": 1}, {"id": 2}],
                     {1: HTTPException(status), 2: {"genres": [{"id": 5}]}})
    with caplog.at_level(logging.WARNING, logger=mdlaw.log.name):
        out = call("browse_by_genre", 5, source="trending")
    assert out == [{"genres": [{"id": 5}]}]
    assert f"skipping title 1 (HTTP {status})" in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 428])
def test_browse_by_genre_stops_on_session_wide_errors(upstream, status):
    _detail_upstream(upstream, [{"id": 1}, {"id": 2}],
                     {1: HTTPException(status), 2: {"genres": [{"id": 5}]}})
    with pytest.raises(HTTPException) as ei:
        call("browse_by_genre", 5, source="top_movies")
    assert ei.value.status_code == status
    assert _detail_ids(upstream) == [1]   # no further detail requests


# --- lifecycle -----------------------------------------------------------

def test_close_closes_the_loops_httpx_client(monkeypatch):
    monkeypatch.setattr(mdlaw.client, "TRANSPORT", "httpx")

    async def go():
        c = mdlaw.client.get_client()
        await mdlaw.MDL().close()
        return c
    assert asyncio.run(go()).is_closed


def test_stats_reports_shared_cache():
    mdlaw.caching.cache.put("k", {"v": 1}, 60)
    mdlaw.caching.cache.get("k")
    mdlaw.caching.cache.get("missing")
    s = mdlaw.MDL().stats()
    assert (s["hits"], s["misses"], s["entries"]) == (1, 1, 1)


# --- edge cases: limit, transport, path segments --------------------------

@pytest.mark.parametrize("limit", [0, -1, None])
def test_no_limit_means_no_cap_everywhere(upstream, limit):
    """limit of 0/None/negative means "no cap" for search and browse_by_genre
    alike (before: search returned all, browse_by_genre [], trending 1)."""
    rows = [{"id": i} for i in range(1, 4)]
    upstream.reply = rows
    assert call("search", "x", limit=limit) == rows
    assert call("browse_by_genre", 18, limit=limit) == rows
    details = {i: {"id": i, "genres": [{"id": 5}]} for i in range(1, 4)}
    _detail_upstream(upstream, rows, details)
    assert call("browse_by_genre", 5, limit=limit, source="trending") == list(details.values())


def test_browse_by_genre_skips_non_dict_candidates_and_genres(upstream):
    pool = ["junk", None, {"id": 1}]
    _detail_upstream(upstream, pool, {1: {"genres": ["drama", {"id": 5}]}})
    assert call("browse_by_genre", 5, source="trending") == [{"genres": ["drama", {"id": 5}]}]


@pytest.mark.parametrize("given, stored", [("CURL_CFFI", "curl_cffi"), (" HTTPX ", "httpx")])
def test_constructor_normalises_transport(monkeypatch, given, stored):
    monkeypatch.setattr(mdlaw.client, "TRANSPORT", "httpx")
    mdlaw.MDL(transport=given)
    assert mdlaw.client.TRANSPORT == stored


def test_constructor_rejects_unknown_transport(monkeypatch):
    monkeypatch.setattr(mdlaw.client, "TRANSPORT", "httpx")
    with pytest.raises(ValueError):
        mdlaw.MDL(transport="requests")
    assert mdlaw.client.TRANSPORT == "httpx"


@pytest.mark.parametrize("name, value, path", [
    ("user_lists", "../titles/1", "/users/..%2Ftitles%2F1/lists"),
    ("user_friends", "a b?x=1", "/users/a%20b%3Fx%3D1/friends"),
    ("title", "1/reviews", "/titles/1%2Freviews?expand=1"),
    ("user_lists", "alice", "/users/alice/lists"),
])
def test_path_segments_are_escaped(upstream, name, value, path):
    call(name, value)
    assert upstream.calls[-1][1] == path


@pytest.mark.parametrize("value", ["", " ", ".", ".."])
def test_path_segment_rejects_empty_and_dot_segments(upstream, value):
    with pytest.raises(HTTPException) as ei:
        call("user_lists", value)
    assert ei.value.status_code == 400 and upstream.calls == []
