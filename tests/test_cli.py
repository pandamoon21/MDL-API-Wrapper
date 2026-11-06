"""Offline CLI tests: output modes, renderers, errors, logging, exit codes."""
import json
import time

import httpx
import pytest
from fastapi import HTTPException

import mdlaw

PERSON = {
    "id": 14352, "name": "Chen Zhe Yuan", "original_name": "陈哲远",
    "biography": "Chen Zhe Yuan is a Chinese actor.", "birthday": "1996-10-29",
    "dod": "0000-00-00", "nationality": "Chinese", "flowers": 946582, "ranked": 1,
    "gender": "Male", "permalink": "https://mydramalistcom/people/14352-chen-zhe-yuan",
    "slug": "people/14352-chen-zhe-yuan",
}
LEADERBOARD = {"time_period": "weekly", "entries": [
    {"id": 10 + i, "display_name": f"Actor {i}", "nationality": "Korean",
     "total_flowers": 5000 - i, "ranked": i, "previous_rank": i + 1} for i in range(1, 31)]}


def cli(monkeypatch, capsys, argv, **methods):
    """Run main(argv) with MDL methods stubbed; returns (code, out, err)."""
    for name, value in methods.items():
        async def stub(self, *a, _v=value, **k):
            if isinstance(_v, BaseException):
                raise _v
            return _v
        monkeypatch.setattr(mdlaw.MDL, name, stub)
    code = mdlaw.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# --- output modes ---------------------------------------------------------

def test_piped_output_defaults_to_json(monkeypatch, capsys):
    code, out, _ = cli(monkeypatch, capsys, ["genres"], genres=[{"id": 1, "name": "Action"}])
    assert code == 0 and json.loads(out) == [{"id": 1, "name": "Action"}]


def test_text_output_is_a_table(monkeypatch, capsys):
    code, out, _ = cli(monkeypatch, capsys, ["genres", "-o", "text"],
                       genres=[{"id": 1, "name": "Action"}, {"id": 5, "name": "Adventure"}])
    lines = out.splitlines()
    assert code == 0 and lines[0].split() == ["ID", "Genre"]
    assert "Adventure" in out and "2 genres" in out


def test_global_options_work_before_or_after_command(monkeypatch, capsys):
    _, a, _ = cli(monkeypatch, capsys, ["-o", "text", "genres"], genres=[{"id": 1, "name": "X"}])
    _, b, _ = cli(monkeypatch, capsys, ["genres", "-o", "text"], genres=[{"id": 1, "name": "X"}])
    assert a == b and "Genre" in a


# --- renderers ------------------------------------------------------------

def test_people_card(monkeypatch, capsys):
    _, out, _ = cli(monkeypatch, capsys, ["people", "14352", "-o", "text"], people=PERSON)
    assert out.startswith("Chen Zhe Yuan (陈哲远)")
    assert "946,582" in out and "1996-10-29" in out
    assert "https://mydramalist.com/people/14352-chen-zhe-yuan" in out  # not the broken permalink
    assert "0000-00-00" not in out  # placeholder dates are hidden


def test_leaderboard_default_limit_and_moves(monkeypatch, capsys):
    _, out, _ = cli(monkeypatch, capsys, ["leaderboard", "weekly", "-o", "text"],
                    leaderboard=LEADERBOARD)
    assert "showing 20 of 30" in out and "▲1" in out
    _, out, _ = cli(monkeypatch, capsys, ["leaderboard", "-n", "0", "-o", "text"],
                    leaderboard=LEADERBOARD)
    assert "Actor 30" in out and "showing" not in out


def test_search_table_and_next_page_hint(monkeypatch, capsys):
    items = [{"id": 686, "title": "Crash Landing on You", "year": 2019, "type": "Drama",
              "country": "South Korea", "rating": 9.0}]
    _, out, _ = cli(monkeypatch, capsys, ["search", "crash", "landing", "-o", "text"], search=items)
    assert "Crash Landing on You" in out and "2019" in out and "9.0" in out
    assert "mdlaw search 'crash landing' --page 2" in out


def test_title_card_with_unknown_shape_is_robust(monkeypatch, capsys):
    detail = {"id": 686, "title": "Crash Landing on You", "year": 2019,
              "genres": [{"id": 19, "name": "Romance"}, {"id": 17, "name": "Comedy"}],
              "synopsis": "A paragliding mishap..."}
    _, out, _ = cli(monkeypatch, capsys, ["title", "686", "-o", "text"], title=detail)
    assert out.startswith("Crash Landing on You (2019)")
    assert "Romance, Comedy" in out and "paragliding" in out


def test_calendar_groups_by_day(monkeypatch, capsys):
    now = time.time()
    data = {"items": [{"id": 1, "rid": 1, "episode_number": 3, "released_at": now + 60},
                      {"id": 2, "rid": 2, "episode_number": 0, "released_at": now + 86400 * 30}],
            "relationships": [{"id": 1, "title": "Soon Show", "country": "Japan"},
                              {"id": 2, "title": "Far Movie", "type": "Movie"}]}
    _, out, _ = cli(monkeypatch, capsys, ["calendar", "--days", "2", "-o", "text"], calendar=data)
    assert "Soon Show" in out and "ep 3" in out
    assert "Far Movie" not in out and "1 releases in the next 2 day(s)" in out


def test_generic_renderer_for_unknown_list(monkeypatch, capsys):
    data = {"items": [{"id": 1, "title": "A", "status": "watching", "image": "http://x"}]}
    _, out, _ = cli(monkeypatch, capsys, ["watchlist", "-o", "text"], watchlist=data)
    assert "A" in out and "watching" in out and "http" not in out


def test_cjk_width_alignment():
    assert mdlaw._dw("陈哲远") == 6
    assert mdlaw._dw(mdlaw._fit("陈哲远abc", 5)) == 5
    rows = mdlaw._table(["Name", "X"], [["陈哲远", "1"], ["abc", "2"]])
    # same display column even though the CJK row has fewer characters
    assert mdlaw._dw(rows[2][:rows[2].index("1")]) == rows[3].index("2")


# --- errors and exit codes ------------------------------------------------

@pytest.mark.parametrize("argv", [["title", "abc"], ["search", "x", "--page"],
                                  ["leaderboard", "bogus"], ["watchlist", "nope"]])
def test_usage_errors_exit_2_without_traceback(monkeypatch, capsys, argv):
    code, _, err = cli(monkeypatch, capsys, argv)
    assert code == 2 and "error:" in err and "hint:" in err and "Traceback" not in err


def test_unknown_command_suggests(monkeypatch, capsys):
    code, _, err = cli(monkeypatch, capsys, ["serch", "x"])
    assert code == 2 and "did you mean `mdlaw search`" in err


def test_api_error_is_explained(monkeypatch, capsys):
    e = HTTPException(400, {"error": True, "code": 400,
                            "detail": "this endpoint requires MDL_USERNAME and MDL_PASSWORD env vars"})
    code, out, err = cli(monkeypatch, capsys, ["me"], me=e)
    assert code == 1 and out == ""
    assert "needs a MyDramaList account" in err and "mdlaw auth" in err
    assert "{'error'" not in err


def test_404_is_explained(monkeypatch, capsys):
    e = HTTPException(404, {"error": True, "code": 404, "detail": {"code": 404, "message": "Not Found"}})
    code, _, err = cli(monkeypatch, capsys, ["people", "9"], people=e)
    assert code == 1 and "not found" in err and "check the id" in err


def test_unexpected_error_is_short_unless_vv(monkeypatch, capsys):
    code, _, err = cli(monkeypatch, capsys, ["genres"], genres=RuntimeError("boom"))
    assert code == 1 and "unexpected RuntimeError: boom" in err and "Traceback" not in err
    code, _, err = cli(monkeypatch, capsys, ["-vv", "genres"], genres=RuntimeError("boom"))
    assert "Traceback" in err


def test_help_and_version_exit_0(monkeypatch, capsys):
    assert cli(monkeypatch, capsys, ["--help"])[0] == 0
    code, out, _ = cli(monkeypatch, capsys, ["--version"])
    assert code == 0 and mdlaw.__version__ in out


# --- logging --------------------------------------------------------------

class _Client:
    async def request(self, method, url, headers=None, json=None):
        return httpx.Response(200, content=b'[{"id": 1, "name": "Action"}]')


def test_verbose_logs_requests_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(mdlaw, "get_client", lambda: _Client())
    code, out, err = cli(monkeypatch, capsys, ["-v", "genres"])
    assert code == 0 and json.loads(out)[0]["name"] == "Action"
    assert "GET /genres -> 200" in err and "ms" in err
    assert "cache" not in err  # cache detail is -vv only


def test_quiet_by_default(monkeypatch, capsys):
    monkeypatch.setattr(mdlaw, "get_client", lambda: _Client())
    _, _, err = cli(monkeypatch, capsys, ["genres"])
    assert err == ""


def test_vv_logs_cache_activity(monkeypatch, capsys):
    monkeypatch.setattr(mdlaw, "get_client", lambda: _Client())
    _, _, err = cli(monkeypatch, capsys, ["-vv", "genres"])
    assert "cache miss: GET /genres" in err


# --- auth commands ----------------------------------------------------------

def test_auth_status_not_logged_in(monkeypatch, capsys):
    code, out, _ = cli(monkeypatch, capsys, ["auth", "status", "-o", "text"])
    assert code == 0 and "Not logged in" in out and "mdlaw auth" in out


def test_auth_status_expired_json_has_no_tokens(tmp_path, monkeypatch, capsys):
    (tmp_path / ".mdlaw_auth.json").write_text(json.dumps({
        "token": "SECRET-T", "refresh_token": "SECRET-R", "expires_at": time.time() - 7200,
        "username": "me@x.com"}))
    code, out, _ = cli(monkeypatch, capsys, ["auth", "status", "--json"])
    info = json.loads(out)
    assert code == 0 and info["expired"] is True and info["can_refresh"] is True
    assert info["account"] == "me@x.com" and "SECRET" not in out


def test_auth_without_input_in_non_interactive_mode(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.delenv("MDL_USERNAME", raising=False)
    code, _, err = cli(monkeypatch, capsys, ["auth"])
    assert code == 2 and "no username given" in err


def test_logout_without_session(monkeypatch, capsys):
    code, out, _ = cli(monkeypatch, capsys, ["logout"])
    assert code == 0 and "Not logged in" in out


def test_server_logging_level_and_format(monkeypatch):
    import logging
    monkeypatch.setenv("MDL_LOG_LEVEL", "debug")
    mdlaw._setup_server_logging()
    assert mdlaw.log.level == logging.DEBUG and not mdlaw.log.propagate
    rec = logging.LogRecord("mdlaw", logging.INFO, __file__, 1, "GET /x -> 200", None, None)
    assert mdlaw.log.handlers[0].format(rec) == "INFO:     mdlaw: GET /x -> 200"
    mdlaw._setup_server_logging()  # idempotent: no second handler
    assert len(mdlaw.log.handlers) == 1


def test_watchlist_uses_list_id_and_episode_progress(monkeypatch, capsys):
    """Shape of /sync/mylist/<status> (verified against a real account)."""
    items = [
        {"list_id": 1, "episode_seen": 5, "rating": 0,
         "title": {"id": 769141, "title": "Bloodhounds Season 2", "year": 2026, "episodes": 7}},
        {"list_id": 1, "episode_seen": 14, "rating": 8,
         "title": {"id": 778376, "title": "Phantom Lawyer", "year": 2026, "episodes": 16}},
    ]
    _, out, _ = cli(monkeypatch, capsys, ["watchlist", "-o", "text"], watchlist=items)
    assert "5/7" in out and "14/16" in out
    assert "2 titles in your watching list" in out
    assert "Status" not in out  # single list → no redundant status column
    mixed = items + [{"list_id": 2, "episode_seen": 8, "rating": 9.5,
                      "title": {"id": 1, "title": "Done", "episodes": 8}}]
    _, out, _ = cli(monkeypatch, capsys, ["watchlist", "-o", "text"], watchlist=mixed)
    assert "completed" in out and "9.5" in out


def test_title_card_real_fields(monkeypatch, capsys):
    """Shape of /titles/{id}?expand=1 (verified live): ranked/popularity,
    release_dates_fmt, runtime, tags, alt_titles; 99999 means unranked."""
    detail = {"id": 686, "title": "Karei naru Spy", "year": 2009, "episodes": 10, "runtime": 45,
              "rating": 6.9, "votes": 334, "ranked": 11994, "popularity": 99999,
              "release_dates_fmt": "Jul 18, 2009 - Sep 26, 2009",
              "alt_titles": ["A", "B", "C", "D"], "tags": [{"name": "Espionage"}],
              "genres": [{"name": "Action"}], "permalink": "https://mydramalist.com/686-karei-naru-spy"}
    _, out, _ = cli(monkeypatch, capsys, ["title", "686", "-o", "text"], title=detail)
    assert "10 × 45 min" in out and "#11,994" in out and "334 votes" in out
    assert "Popularity" not in out  # 99999 = not ranked
    assert "A, B, C …" in out and "Espionage" in out and "Jul 18, 2009" in out


def test_cli_browse_builds_filters_and_next_page(monkeypatch, capsys):
    seen = {}

    async def fake_browse(self, **kw):
        seen.update(kw)
        return [{"id": 35729, "title": "Crash Landing on You", "year": 2019,
                 "media_type": "Korean Drama", "country": "South Korea", "rating": 9.0}]
    monkeypatch.setattr(mdlaw.MDL, "browse", fake_browse)
    code = mdlaw.main(["browse", "--type", "drama", "--genre", "romance", "--country", "korea",
                       "--from", "2019", "--sort", "popular", "-o", "text"])
    out = capsys.readouterr().out
    assert code == 0 and "Crash Landing on You" in out
    assert seen == {"types": ["drama"], "genres": ["romance"], "countries": ["korea"],
                    "released_from": 2019, "sort": "popular", "page": 1}
    assert ("next page: mdlaw browse --type drama --genre romance --country korea "
            "--from 2019 --sort popular --page 2") in out


def test_cli_browse_rejects_unknown_type(monkeypatch, capsys):
    code, _, err = cli(monkeypatch, capsys, ["browse", "--type", "anime"])
    assert code == 2 and "invalid choice" in err


def test_cli_lists(monkeypatch, capsys):
    lists = [{"id": 1, "name": "Best Romance", "count": 42, "total_likes": 1200, "total_views": 99000}]
    _, out, _ = cli(monkeypatch, capsys, ["lists", "-o", "text"], lists_trending=lists)
    assert "Best Romance" in out and "1,200" in out and "99,000" in out


def test_cli_browse_unknown_country_is_usage_error(monkeypatch, capsys):
    code, _, err = cli(monkeypatch, capsys, ["browse", "--country", "france"])
    assert code == 2 and "unknown country 'france'" in err and "HTTP" not in err


def test_cli_recommendations(monkeypatch, capsys):
    recs = [{"id": 1, "content": "Same director, same narrative style.", "total_likes": 1200,
             "author": {"display_name": "fan"},
             "title": {"id": 10826, "title": "Signal", "year": 2016},
             "similar": {"id": 7251, "title": "Tunnel", "year": 2017}}]
    _, out, _ = cli(monkeypatch, capsys, ["recs", "-o", "text"], recommendations=recs)
    assert "Liked Signal (2016) [10826]" in out and "→ try Tunnel (2017) [7251]" in out
    assert "Same director" in out and "by fan · 1,200 likes" in out
    assert "mdlaw recommendations --page 2" in out
