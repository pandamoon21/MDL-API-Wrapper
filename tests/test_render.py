"""Pure-function tests for mdlaw.render: layout helpers, renderers, error text.

No network and no CLI round-trip: each renderer gets data plus a Namespace
standing in for parsed arguments. The terminal width is pinned so table and
card layout is deterministic.
"""
import os
import time
from argparse import Namespace

import pytest
from fastapi import HTTPException

from mdlaw import render as r


@pytest.fixture(autouse=True)
def _width(monkeypatch):
    monkeypatch.setattr(r, "_term_width", lambda: 100)


def args(**kw) -> Namespace:
    base = dict(limit=None, page=1, query=["q"], days=3, which="trending", period="weekly",
                status=None, id=7, types=None, genres=None, countries=None,
                released_from=None, sort="top")
    base.update(kw)
    return Namespace(**base)


# --- width and fit --------------------------------------------------------

@pytest.mark.parametrize("cols, expected", [(30, 40), (40, 40), (180, 180)])
def test_term_width_has_a_floor_of_40(monkeypatch, cols, expected):
    monkeypatch.undo()  # use the real _term_width
    monkeypatch.setattr(r.shutil, "get_terminal_size", lambda fallback: os.terminal_size((cols, 24)))
    assert r._term_width() == expected


@pytest.mark.parametrize("text, width", [
    ("abc", 3), ("陈哲远", 6), ("Ｆｕｌｌ", 8), ("é", 1), ("", 0), ("a陈b", 4)])
def test_display_width(text, width):
    assert r._dw(text) == width


@pytest.mark.parametrize("text, width, expected", [
    ("abc", 5, "abc  "),            # padded
    ("abcdef", 4, "abc…"),          # truncated, ellipsis counts as one column
    ("陈哲远abc", 5, "陈哲…"),       # two wide chars + …
    ("陈哲远abc", 4, "陈… "),        # next wide char would overflow: pad instead
    ("陈哲远", 6, "陈哲远"),          # exact fit, no ellipsis
])
def test_fit_truncates_by_display_columns(text, width, expected):
    out = r._fit(text, width)
    assert out == expected and r._dw(out) == width


# --- table ----------------------------------------------------------------

def test_table_fits_without_shrinking():
    lines = r._table(["ID", "Title"], [[1, "Short"], [22, None]])
    assert lines == ["ID  Title", "──  ─────", "1   Short", "22"]


def test_table_shrinks_flex_column_to_terminal(monkeypatch):
    monkeypatch.setattr(r, "_term_width", lambda: 40)
    lines = r._table(["ID", "Title"], [[1, "x" * 60]])
    assert lines[1] == "──  " + "─" * 36
    assert lines[2] == "1   " + "x" * 35 + "…"
    assert all(r._dw(line) <= 40 for line in lines)


def test_table_flex_column_never_below_12(monkeypatch):
    monkeypatch.setattr(r, "_term_width", lambda: 40)
    lines = r._table(["Name", "T"], [["a" * 35, "b" * 30]], flex=1)
    # the non-flex column keeps its width, so the line overflows the terminal
    assert lines[2] == "a" * 35 + "  " + "b" * 11 + "…"


def test_table_flex_selects_the_shrinking_column(monkeypatch):
    monkeypatch.setattr(r, "_term_width", lambda: 40)
    lines = r._table(["Name", "Note"], [["n" * 30, "m" * 20]], flex=0)
    assert lines[2] == "n" * 17 + "…" + "  " + "m" * 20


# --- card -----------------------------------------------------------------

def test_card_with_subtitle_fields_and_body():
    lines = r._card("Title", [("Name", "X"), ("Empty", None), ("Born", "0000-00-00")],
                    body="first line\r\n\nsecond", subtitle="sub")
    assert lines == ["Title", "sub", "", "Name  X", "", "first line", "", "second"]


def test_card_wraps_long_values_under_the_value_column(monkeypatch):
    monkeypatch.setattr(r, "_term_width", lambda: 40)
    lines = r._card("T", [("Key", "word " * 12)])
    assert lines[2].startswith("Key  word")
    assert len(lines) > 3 and all(line.startswith("     word") for line in lines[3:])


def test_card_without_displayable_fields_is_title_only():
    assert r._card("T", [("A", None), ("B", [])]) == ["T"]


# --- scalar helpers -------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, ""), ("", ""), ("0000-00-00", ""), ("0001-01-01", ""), ([], ""), ({}, ""),
    (True, "yes"), (False, "no"), (0, "0"), (2.50, "2.5"), (1e20, "1e+20"),
    ([{"name": "A"}, "B", 3], "A, B, 3"), (("x", "y"), "x, y"),
    ("  a \n b\t", "a b"),
])
def test_clean(value, expected):
    assert r._clean(value) == expected


@pytest.mark.parametrize("value, expected", [
    (1234567, "1,234,567"), (5, "5"), (0, None), (None, None), (True, None), ("12", None)])
def test_num(value, expected):
    assert r._num(value) == expected


def test_pick_skips_empty_values_in_key_order():
    d = {"a": "", "b": "0000-00-00", "c": 0, "d": "x"}
    assert r._pick(d, "a", "b", "c", "d") == 0          # 0 is a value, not empty
    assert r._pick(d, "a", "b", "missing") is None


@pytest.mark.parametrize("data, keys, expected", [
    ([1, 2], (), [1, 2]),
    ({"items": [1]}, (), [1]),
    ({"lists": [2], "items": [1]}, ("lists",), [2]),     # named key wins
    ({"items": "not a list", "data": [3]}, (), [3]),
    ({"other": [1]}, (), []),
    ("scalar", (), []),
    (None, (), []),
])
def test_items(data, keys, expected):
    assert r._items(data, *keys) == expected


@pytest.mark.parametrize("n_rows, limit, default, kept, note", [
    (5, None, None, 5, None),                                  # no limit, no default
    (5, None, 3, 3, "showing 3 of 5 · -n 0 shows all"),        # default applies
    (5, 2, 3, 2, "showing 2 of 5 · -n 0 shows all"),           # explicit limit wins
    (5, 0, 3, 5, None),                                        # 0 = all
    (5, 5, None, 5, None),                                     # exactly at the limit
    (5, 9, None, 5, None),
    (0, 3, 3, 0, None),
])
def test_limited(n_rows, limit, default, kept, note):
    rows, more = r._limited(list(range(n_rows)), limit, default)
    assert rows == list(range(kept)) and more == note


@pytest.mark.parametrize("slug, expected", [
    (None, None), ("", None),
    ("people/1-x", "https://mydramalist.com/people/1-x"),
    ("/686-crash", "https://mydramalist.com/686-crash"),
    ("https://example.org/a", "https://example.org/a"),
])
def test_mdl_url(slug, expected):
    assert r._mdl_url(slug) == expected


@pytest.mark.parametrize("value, expected", [
    (1, "#1"), (11994, "#11,994"), (99998, "#99,998"),
    (99999, None), (0, None), (-3, None), (None, None), ("5", None), (5.0, None)])
def test_rank(value, expected):
    assert r._rank(value) == expected


@pytest.mark.parametrize("d, expected", [
    ({"year": 2019}, "2019"),
    ({"released_year": 2001, "released": "1999-01-01"}, "2001"),
    ({"year": 0, "released": "2018-05-01"}, "2018"),     # 0 year falls back to a date
    ({"aired_start": "2020-02-02"}, "2020"),
    ({"released": "0000-00-00"}, ""),
    ({}, ""),
])
def test_year(d, expected):
    assert r._year(d) == expected


@pytest.mark.parametrize("d, expected", [
    ({"rating": 9}, "9.0"), ({"score": "8.26"}, "8.3"), ({"rating": 0}, ""),
    ({"rating": -1}, ""), ({"rating": "n/a"}, ""), ({"rating": [1]}, ""),
    ({"rating": None, "score": 7.5}, "7.5"), ({}, ""),
])
def test_rating(d, expected):
    assert r._rating(d) == expected


def test_title_table_columns():
    assert r._title_table([{"id": 1, "name": "N", "released": "2010-01-01",
                            "content_type": "Movie", "language": "ko", "score": 7}]) == \
        [[1, "N", "2010", "Movie", "ko", "7.0"]]


@pytest.mark.parametrize("kw, expected", [
    ({}, "mdlaw browse --page 2"),
    ({"types": ["drama"], "genres": ["slice of life"], "countries": ["korea"],
      "released_from": 2020, "sort": "popular"},
     "mdlaw browse --type drama --genre 'slice of life' --country korea --from 2020 "
     "--sort popular --page 2"),
])
def test_browse_cmd(kw, expected):
    assert r._browse_cmd(args(**kw), 2) == expected


# --- renderers with empty data --------------------------------------------

@pytest.mark.parametrize("fn, kw, expected", [
    (r._render_calendar, {"days": 3}, ["Nothing scheduled in the next 3 day(s)."]),
    (r._render_search, {}, ["No titles found."]),
    (r._render_browse, {}, ["No titles match these filters."]),
    (r._render_browse, {"page": 2}, ["No titles match these filters. Try an earlier page."]),
    (r._render_recommendations, {}, ["No recommendations."]),
    (r._render_lists, {}, ["No lists."]),
    (r._render_search_people, {}, ["No people found."]),
    (r._render_watchlist, {}, ["Nothing in your watching list."]),
    (r._render_watchlist, {"status": "dropped"}, ["Nothing in your dropped list."]),
])
@pytest.mark.parametrize("data", [[], {}, {"items": []}, None])
def test_renderers_with_no_items(fn, kw, expected, data):
    assert fn(data, args(**kw)) == expected


@pytest.mark.parametrize("fn, summary", [
    (r._render_genres, "0 genres"), (r._render_languages, "0 languages")])
def test_count_renderers_with_no_items(fn, summary):
    out = fn([], args())
    assert len(out) == 4 and out[-1] == summary   # header, rule, blank, count


def test_leaderboard_with_no_entries_is_header_only():
    out = r._render_leaderboard({"entries": []}, args())
    assert out[0].split() == ["#", "Move", "Name", "Nationality", "Flowers", "ID"]
    assert out[2:] == ["", "weekly leaderboard · profile: mdlaw people <ID>"]


@pytest.mark.parametrize("fn", [r._render_title, r._render_people, r._render_profile])
@pytest.mark.parametrize("data, expected", [
    ([], ["No results."]), ("plain", ["plain"]), (["a", "b"], ["a", "b"])])
def test_card_renderers_fall_back_to_generic_for_non_dicts(fn, data, expected):
    assert fn(data, args()) == expected


def test_profile_with_no_known_fields_falls_back_to_generic():
    assert r._render_profile({}, args()) == ["(no displayable fields — try --json)"]
    assert r._render_profile({"bio_html": "x"}, args()) == ["Bio html  x"]


# --- renderers with data --------------------------------------------------

def test_languages_table():
    out = r._render_languages({"languages": [{"code": "ko", "name": "Korean", "native_name": "한국어"},
                                             "junk"]}, args())
    assert out[2].split() == ["ko", "Korean", "한국어"] and out[-1] == "1 languages"


def test_search_people_table_and_next_page():
    data = [{"id": 1, "display_name": "Kim", "native_name": "김", "country": "KR"}, "junk"]
    out = r._render_search_people(data, args(query=["kim", "soo"], page=3, limit=None))
    assert out[2].split() == ["1", "Kim", "김", "KR"]
    assert out[-1] == "page 3 · profile: mdlaw people <ID> · next page: " \
                      "mdlaw search-people 'kim soo' --page 4"


def test_search_and_browse_append_limit_note():
    items = [{"id": i, "title": f"T{i}"} for i in range(4)]
    assert r._render_search(items, args(limit=2))[-1] == "showing 2 of 4 · -n 0 shows all"
    out = r._render_browse(items, args(limit=2, sort="popular"))
    assert out[-2] == "next page: mdlaw browse --sort popular --page 2"
    assert out[-1] == "showing 2 of 4 · -n 0 shows all"


def test_lists_table_counts():
    data = {"lists": [{"id": 9, "title": "Best", "count": 1200, "total_likes": 0,
                       "total_views": 45000}]}
    out = r._render_lists(data, args(which="popular"))
    assert out[2].split() == ["9", "Best", "1,200", "45,000"]
    assert out[-1] == "popular lists · -n N for more"


def test_recommendations_block():
    data = [{"title": {"id": 1, "title": "A", "year": 2019},
             "similar": {"id": 2, "title": "B"},
             "content": "Both are slow-burn romances.",
             "author": {"display_name": "fan"}, "total_likes": 1500},
            {"title": "not a dict", "similar": None}]
    out = r._render_recommendations(data, args(page=2))
    assert out[:5] == ['Liked A (2019) [1]', '  → try B [2]',
                       '    "Both are slow-burn romances."', '    by fan · 1,500 likes', '']
    assert out[5:8] == ["Liked  [None]", "  → try  [None]", ""]
    assert out[-1] == "page 2 · details: mdlaw title <ID> · next: mdlaw recommendations --page 3"


def test_title_card_fields():
    data = {"title": "Show", "released": "2020-01-01", "genres": "Drama, Romance",
            "alt_titles": ["a", "b", "c", "d"], "episodes": 16, "runtime": 60,
            "aired_start": "2020-01-01", "aired_end": "2020-03-01",
            "rating": 8.5, "votes": 12000, "ranked": 99999, "popularity": 42,
            "url": "686-show", "synopsis": "Plot."}
    out = r._render_title(data, args())
    assert out[0] == "Show (2020)"
    body = "\n".join(out)
    for expected in ("Also known as  a, b, c …", "Episodes       16 × 60 min",
                     "Aired          2020-01-01 – 2020-03-01", "Rating         8.5/10 (12,000 votes)",
                     "Popularity     #42", "Genres         Drama, Romance",
                     "Link           https://mydramalist.com/686-show"):
        assert expected in body
    assert "Ranked" not in body and out[-1] == "Plot."


def test_title_card_without_name_uses_id():
    assert r._render_title({"status": "Ongoing"}, args(id=55))[0] == "title 55"


def test_people_card_falls_back_to_permalink_and_id():
    out = r._render_people({"permalink": "people/3", "ranked": 7}, args(id=3))
    assert out[0] == "person 3"
    assert "Rank  #7" in out and "Link  https://mydramalist.com/people/3" in out


def test_profile_card():
    data = {"display_name": "Panda", "username": "panda21", "followers": 1500,
            "joined_at": "2019-04-01T10:00:00Z", "vip": True,
            "roles": [{"name": "Editor"}, "junk"], "about": "Hi."}
    out = r._render_profile(data, args())
    assert out[0] == "Panda"
    for expected in ("Username   panda21", "Joined     2019-04-01", "Followers  1,500",
                     "Roles      Editor", "VIP        yes"):
        assert expected in out
    assert out[-1] == "Hi."


def test_profile_hides_username_equal_to_name():
    out = r._render_profile({"username": "same", "location": "Seoul"}, args())
    assert out[0] == "same" and not any(line.startswith("Username") for line in out)


def test_watchlist_shows_status_column_only_for_mixed_lists():
    mixed = {"list": [{"title": {"id": 1, "title": "A", "episodes": 16}, "list_id": 1,
                       "episode_seen": 4, "rating": 8.5},
                      {"id": 2, "title": "B", "status": "custom", "episode_seen": "?"}]}
    out = r._render_watchlist(mixed, args())
    assert out[0].split() == ["ID", "Title", "Year", "Status", "Progress", "Score"]
    assert out[2].split() == ["1", "A", "watching", "4/16", "8.5"]
    assert out[3].split() == ["2", "B", "custom", "?"]
    assert out[-1].startswith("other lists:")

    single = {"list": [{"title": {"id": 1, "title": "A"}, "list_id": 2, "rating": 0}]}
    out = r._render_watchlist(single, args(status="completed"))
    assert "Status" not in out[0]
    assert out[-1] == "1 titles in your completed list · details: mdlaw title <ID>"


def test_calendar_separates_days():
    now = time.time()
    data = {"items": [{"id": 1, "rid": 1, "episode_number": 2, "released_at": now + 60},
                      {"id": 2, "rid": 2, "released_at": now + 86400}],
            "relationships": [{"id": 1, "title": "Daily", "country": "Korea"},
                              {"id": 2, "title": "Film", "type": "Movie"}]}
    out = r._render_calendar(data, args(days=3))
    days = [i for i, line in enumerate(out) if line and not line.startswith(" ")][:2]
    assert len(days) == 2 and out[days[1] - 1] == ""       # blank line between days
    assert any("Film" in line and "Movie" in line for line in out)
    assert out[-1].startswith("2 releases in the next 3 day(s)")


# --- generic renderer -----------------------------------------------------

def test_generic_list_of_dicts_is_a_table_with_known_columns():
    data = [{"id": 1, "name": "Alpha", "rating": 7}, {"rid": 2, "title": "Beta"}]
    out = r._render_generic(data, Namespace())          # no limit attribute at all
    assert out[0].split() == ["ID", "Name", "Rating"]
    assert out[2].split() == ["1", "Alpha", "7"] and out[3].split() == ["2", "Beta"]


def test_generic_table_respects_limit():
    out = r._render_generic({"items": [{"id": i} for i in range(5)]}, args(limit=2))
    assert out[-1] == "showing 2 of 5 · -n 0 shows all" and len(out) == 5


def test_generic_dict_skips_urls_and_nested_data():
    data = {"user_name": "x", "count": 3, "avatar": "https://img", "meta": {"a": 1},
            "tags": ["t"], "empty": None}
    assert r._render_generic(data, args()) == ["User name  x", "Count      3"]


@pytest.mark.parametrize("data, expected", [
    ({"only": {"nested": 1}}, ["(no displayable fields — try --json)"]),
    ([], ["No results."]),
    (["a", 2, None], ["a", "2", ""]),
    (5, ["5"]),
    (True, ["yes"]),
    ("  spaced   text ", ["spaced text"]),
])
def test_generic_non_table_shapes(data, expected):
    assert r._render_generic(data, args()) == expected


def test_renderer_table_covers_every_text_command():
    assert set(r._RENDERERS) == {"genres", "languages", "calendar", "search", "search-people",
                                 "title", "people", "watchlist", "browse", "lists",
                                 "recommendations", "recs", "me", "leaderboard"}
    assert r._RENDERERS["recs"] is r._RENDERERS["recommendations"]


# --- _explain -------------------------------------------------------------

@pytest.mark.parametrize("code, detail, expected", [
    (400, "MDL_USERNAME is required for this endpoint",
     ("this command needs a MyDramaList account",
      "log in once with `mdlaw auth` (or set MDL_USERNAME and MDL_PASSWORD)")),
    (400, "MDL_USERNAME / MDL_PASSWORD not set",
     ("no MyDramaList account configured",
      "log in with `mdlaw auth`, or set MDL_USERNAME and MDL_PASSWORD")),
    (400, "bad filter", ("bad filter (HTTP 400)", None)),
    (401, "session expired — log in again with `mdlaw auth`",
     ("session expired", "log in again with `mdlaw auth`")),
    (401, "invalid token", ("not authorized: invalid token", "log in again with `mdlaw auth`")),
    (403, "blocked by Cloudflare",
     ("Cloudflare blocked the request (your IP looks like a bot to it)",
      "retry with `--transport curl_cffi`, or from another network")),
    (403, "forbidden", ("forbidden (HTTP 403)", None)),
    (404, "gone", ("not found on MyDramaList (HTTP 404)",
                   "check the id — ids come from search results")),
    (428, "2fa", ("this account has two-factor authentication enabled",
                  "mdlaw can't complete 2FA logins; disable 2FA or use an app password")),
    (429, "slow down", ("MyDramaList is rate-limiting requests (HTTP 429)",
                        "wait a minute and try again")),
    (502, "connection refused", ("could not reach MyDramaList: connection refused",
                                 "check your connection and try again")),
    (504, "timeout", ("MyDramaList did not respond in time",
                      "try again; -v shows the request timings")),
    (500, "boom", ("MyDramaList returned an error (HTTP 500)", "try again later")),
    (503, "down", ("MyDramaList returned an error (HTTP 503)", "try again later")),
    (418, "teapot", ("teapot (HTTP 418)", None)),
])
def test_explain_status_codes(code, detail, expected):
    assert r._explain(HTTPException(code, detail)) == expected


@pytest.mark.parametrize("detail, message", [
    ({"detail": "wrapped"}, "wrapped"),
    ({"detail": {"message": "nested"}}, "nested"),
    ({"error_description": "desc", "error": "e"}, "desc"),
    ({"error": "only error"}, "only error"),
    ({"message": "", "error": "fallback"}, "fallback"),
    ({"code": 7, "name": "é"}, '{"code": 7, "name": "é"}'),   # json, non-ASCII kept
    ("multi \n  line\ttext", "multi line text"),
])
def test_explain_extracts_message_from_detail(detail, message):
    assert r._explain(HTTPException(418, detail)) == (f"{message} (HTTP 418)", None)


# --- edge cases ------------------------------------------------------------

def test_rank_rejects_bool():
    assert r._rank(True) is None


@pytest.mark.parametrize("value, expected", [
    ("2019-04-01T10:00:00Z", "2019-04-01"), (1554112800, "2019-04-01"),
    (1554112800.5, "2019-04-01"), (0, ""), (None, ""), (True, "True")])
def test_date(value, expected):
    assert r._date(value) == expected


def test_profile_joined_from_epoch():
    out = r._render_profile({"display_name": "x", "joined_at": 1554112800}, args())
    assert any("2019-04-01" in line for line in out)
    assert not any("1554112800" in line for line in out)


def test_calendar_without_rid_never_borrows_a_show_by_episode_id():
    now = time.time()
    data = {"items": [{"id": 2, "released_at": now + 60, "permalink": "ep-perma"}],
            "relationships": [{"id": 2, "title": "Unrelated show"}]}
    out = "\n".join(r._render_calendar(data, args(days=3)))
    assert "Unrelated show" not in out and "ep-perma" in out


@pytest.mark.parametrize("author, shown", [("fan", "by fan"), (None, None), (42, "by 42")])
def test_recommendations_author_not_a_dict(author, shown):
    data = [{"title": {"id": 1, "title": "A"}, "similar": {"id": 2, "title": "B"},
             "author": author}]
    out = r._render_recommendations(data, args())
    assert (shown in out[2]) if shown else out[2] == ""


def test_generic_list_of_dicts_without_known_columns_hides_repr():
    out = r._render_generic([{"foo": 1}, {"bar": 2}], args())
    assert out == ["2 item(s) with no displayable columns — try --json"]
