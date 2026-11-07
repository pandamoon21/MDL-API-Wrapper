"""Command-line interface: offline self-check, argparse parser, text
renderers and ``main`` (console script ``mdlaw``, ``python -m mdlaw``)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import sys
import textwrap
import time
import unicodedata

from fastapi import HTTPException

from . import caching, client
from ._config import API_KEY, __version__, default_headers, log
from .caching import SQLCache, TTLCache
from .client import BROWSE_COUNTRIES, BROWSE_SORTS, BROWSE_TYPES, _auth, _filter_ids, _fmt_duration, _md5
from .library import MDL

# ---------------------------------------------------------------------------
# Offline self-check
# ---------------------------------------------------------------------------
def self_check() -> int:
    # API_KEY is always set: from MDL_API_KEY env, or a generated nonce.
    assert API_KEY and len(API_KEY) == 20, "API key must be a 20-char nonce"
    assert client.TRANSPORT in ("httpx", "curl_cffi"), f"unknown MDL_TRANSPORT={client.TRANSPORT}"
    h = default_headers()
    assert h["mdl-api-key"] == API_KEY
    assert h["User-Agent"] == "okhttp/4.12.0" and h["Accept"] == "*/*"
    assert "Authorization" not in h
    assert default_headers("t")["Authorization"] == "Bearer t"
    assert _md5("secret") == "5ebe2294ecd0e0f08eab7690d2a6ee69", "md5"
    c = TTLCache()
    c.put("k", {"a": 1}, 100)
    assert c.get("k") == {"a": 1}
    c.put("e", 1, -1)
    assert c.get("e") is None
    assert caching.cache.get("never") is None
    # SQL cache backend (sqlite in-memory): put/get + change detection
    s = SQLCache("sqlite:///:memory:")
    s.put("k", {"a": 1}, 100)
    assert s.get("k") == {"a": 1}
    s.put("k", {"a": 2}, 100)          # changed value
    cur = s._cursor()
    cur.execute("SELECT changed FROM mdlaw_cache WHERE key = 'k'")
    row = cur.fetchone()
    assert row is not None and row[0] == 1, "change detection must flag changed value"
    cur.close()
    s.put("k", {"a": 2}, 100)          # same value
    cur = s._cursor()
    cur.execute("SELECT changed FROM mdlaw_cache WHERE key = 'k'")
    row = cur.fetchone()
    assert row is not None and row[0] == 0, "same value must not flag changed"
    cur.close()
    print(f"PASS: offline checks (key={'env' if os.environ.get('MDL_API_KEY') else 'nonce'} len={len(API_KEY)}, "
          f"transport={client.TRANSPORT}, headers, cache, md5, sqlcache)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# Output: readable tables/cards on a terminal, raw JSON when piped (so
# `mdlaw genres | jq` keeps working) — override with --json / -o text.
# Errors: one "error:" line plus a "hint:" with the next step; exit codes
# 0 ok · 1 API/runtime error · 2 bad usage · 130 interrupted.
# Logs: -v shows each upstream request (status, time, size) and auth events;
# -vv adds cache hits/misses, throttle waits and tracebacks.

_EPILOG = """\
examples:
  mdlaw auth                        log in once (prompts for the password)
  mdlaw search "crash landing"      search titles
  mdlaw title 686                   title detail
  mdlaw people 14352                actor profile
  mdlaw browse --type drama --genre romance --country korea --sort popular
  mdlaw recs                        community picks: "liked X → try Y"
  mdlaw calendar --days 3           episodes airing in the next 3 days
  mdlaw leaderboard weekly -n 10    top 10 actors this week
  mdlaw genres --json | jq '.[].name'
  mdlaw -v me                       show the requests being made

Output is a table on a terminal and JSON when piped; force either with
--json or -o text. Commands marked * need an account (mdlaw auth).
"""

_WATCH_STATUSES = ("completed", "dropped", "onhold", "plantowatch",
                   "notinterested", "undecided")


class _CLIError(Exception):
    """A user-facing failure: message + optional hint, no traceback."""

    def __init__(self, message: str, hint: str | None = None, code: int = 1) -> None:
        super().__init__(message)
        self.message, self.hint, self.code = message, hint, code


# -- argument parsing -------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """argparse with mdlaw-style errors: `error:` + `hint:`, a short usage
    line, and a did-you-mean for mistyped commands."""

    def error(self, message: str) -> None:  # type: ignore[override]
        import difflib
        import re
        m = re.search(r"argument <command>: invalid choice: '([^']*)'", message)
        if m:
            choices = [c for c in self._commands()]
            near = difflib.get_close_matches(m.group(1), choices, n=1)
            _print_error(f"unknown command '{m.group(1)}'",
                         f"did you mean `mdlaw {near[0]}`?" if near
                         else "run `mdlaw --help` to see all commands")
        else:
            print(self.format_usage().rstrip(), file=sys.stderr)
            _print_error(message, f"see `{self.prog} --help`")
        sys.exit(2)

    def _commands(self) -> list[str]:
        for a in self._actions:
            if isinstance(a, argparse._SubParsersAction):
                return list(a.choices)
        return []


def _short_usage(p: argparse.ArgumentParser) -> str:
    """`mdlaw title [options] id` — leave the global options to --help."""
    parts = []
    for a in p._actions:
        if a.option_strings or isinstance(a, argparse._SubParsersAction):
            continue
        name = a.metavar or a.dest
        if a.choices and not a.metavar:
            name = "{" + ",".join(a.choices) + "}"
        parts.append(f"[{name}]" if a.nargs == "?" else
                     f"{name} [{name} ...]" if a.nargs == "+" else name)
    return " ".join(["%(prog)s", "[options]"] + parts)


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {n}")
    return n


def _country(text: str) -> str:
    """Validate --country locally (name, alias or id) so typos are usage errors."""
    try:
        _filter_ids(text, BROWSE_COUNTRIES, "country")
    except HTTPException:
        raise argparse.ArgumentTypeError(
            f"unknown country {text!r} (choose from {', '.join(BROWSE_COUNTRIES)}, "
            "korea, jp, … or an id)") from None
    return text


def _limit(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 (all) or more")
    return n


def _add_output_options(p: argparse.ArgumentParser, sub: bool) -> None:
    """Global options, accepted before or after the command. On subparsers
    the defaults are SUPPRESSed so they don't overwrite the main parser's."""
    d = argparse.SUPPRESS if sub else None
    g = p.add_argument_group("output options")
    g.add_argument("-o", "--output", choices=("auto", "text", "json"),
                   default=d if sub else "auto",
                   help="auto (default): text on a terminal, JSON when piped")
    g.add_argument("--json", dest="output", action="store_const", const="json",
                   default=argparse.SUPPRESS, help="shorthand for -o json")
    g.add_argument("-n", "--limit", type=_limit, default=d,
                   help="max rows in text output (0 = all)")
    g.add_argument("-v", "--verbose", action="count", default=d if sub else 0,
                   help="-v: log requests and auth; -vv: also cache/throttle and tracebacks")
    g.add_argument("--transport", choices=("curl_cffi", "httpx"), default=d,
                   help="upstream HTTP stack (default: MDL_TRANSPORT or curl_cffi)")


def _build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    parser = _Parser(
        prog="mdlaw", formatter_class=fmt, epilog=_EPILOG,
        description="MyDramaList from the command line (unofficial; uses the MDL app API).")
    parser.add_argument("--version", action="version", version=f"mdlaw {__version__}")
    _add_output_options(parser, sub=False)
    sp = parser.add_subparsers(dest="command", metavar="<command>")

    def cmd(name: str, help_: str, **kw) -> argparse.ArgumentParser:
        desc = (help_[2:] + " — needs a MyDramaList account (log in with `mdlaw auth`)"
                if help_.startswith("* ") else help_)
        p = sp.add_parser(name, help=help_, description=desc, formatter_class=fmt, **kw)
        _add_output_options(p, sub=True)
        made.append(p)
        return p
    made: list[argparse.ArgumentParser] = []

    p = cmd("auth", "log in and save the session, or show it (auth status)")
    p.add_argument("username", nargs="?",
                   help="MDL username or email; `status` shows the saved session")
    p.add_argument("password", nargs="?",
                   help="password (avoid: it ends up in shell history — use the prompt)")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin (for scripts)")
    cmd("logout", "remove the saved session")
    cmd("genres", "list all genres")
    cmd("languages", "list supported languages")
    p = cmd("calendar", "upcoming episodes and releases")
    p.add_argument("--days", type=_positive_int, default=7,
                   help="how many days ahead to show in text output (default 7)")
    p = cmd("search", "* search titles")
    p.add_argument("query", nargs="+")
    p.add_argument("--page", type=_positive_int, default=1)
    p.add_argument("--country", help="keep only this country, e.g. 'South Korea'")
    p.add_argument("--type", help="keep only this type, e.g. Drama or Movie")
    p.add_argument("--year", type=_positive_int, help="keep only this release year")
    p = cmd("browse", "* browse the catalog with filters (type, genre, country, year)")
    p.add_argument("--type", dest="types", action="append", choices=list(BROWSE_TYPES),
                   help="repeatable")
    p.add_argument("--genre", dest="genres", action="append", metavar="GENRE",
                   help="genre name or id, e.g. romance; repeatable (see `mdlaw genres`)")
    p.add_argument("--country", dest="countries", action="append", metavar="COUNTRY", type=_country,
                   help=f"{' | '.join(BROWSE_COUNTRIES)}; repeatable")
    p.add_argument("--from", dest="released_from", type=_positive_int, metavar="YEAR",
                   help="released in YEAR or later")
    p.add_argument("--sort", choices=BROWSE_SORTS, default="top",
                   help="top = highest rated (default), popular = most watched")
    p.add_argument("--page", type=_positive_int, default=1)
    p = cmd("recommendations", "community picks: \"if you liked X, watch Y\"", aliases=["recs"])
    p.add_argument("--page", type=_positive_int, default=1)
    p = cmd("lists", "user lists: trending (default), recent, popular, featured")
    p.add_argument("which", nargs="?", default="trending",
                   choices=("trending", "recent", "popular", "featured"))
    p = cmd("search-people", "* search actors and crew")
    p.add_argument("query", nargs="+")
    p.add_argument("--page", type=_positive_int, default=1)
    p = cmd("title", "* title detail")
    p.add_argument("id", type=_positive_int, help="title id (from search results)")
    p = cmd("people", "actor / crew profile")
    p.add_argument("id", type=_positive_int, help="person id")
    p = cmd("watchlist", "* your lists (default: what you're watching)")
    p.add_argument("status", nargs="?", choices=_WATCH_STATUSES,
                   help="which list; omit for the titles you're currently watching")
    cmd("me", "* your profile")
    p = cmd("leaderboard", "most popular actors")
    p.add_argument("period", nargs="?", default="alltime",
                   choices=("alltime", "weekly", "monthly"))
    cmd("self", "offline self-check")
    p = cmd("serve", "start the HTTP API server (default when no command is given)",
            aliases=["run"])
    p.add_argument("--host", help="bind address (default: HOST env or 0.0.0.0)")
    p.add_argument("--port", help="port (default: PORT env or 8000)")
    for p in made:
        p.usage = _short_usage(p)
    return parser


# -- terminal output helpers ------------------------------------------------
def _ensure_utf8_stdio() -> None:
    """Piped output on Windows defaults to the ANSI code page, which can't
    encode Korean/Chinese titles. JSON consumers expect UTF-8 anyway."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def _term_width() -> int:
    return max(40, shutil.get_terminal_size((100, 24)).columns)


def _dw(text: str) -> int:
    """Display width: CJK / full-width characters take two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else
               0 if unicodedata.combining(c) else 1 for c in text)


def _fit(text: str, width: int) -> str:
    """Truncate to ``width`` display columns (with …) and pad to it."""
    if _dw(text) > width:
        out, w = [], 0
        for c in text:
            cw = _dw(c)
            if w + cw > width - 1:
                break
            out.append(c)
            w += cw
        text = "".join(out) + "…"
    return text + " " * (width - _dw(text))


def _table(headers: list[str], rows: list[list[object]], flex: int = 1) -> list[str]:
    """Aligned columns; column ``flex`` shrinks to fit the terminal width."""
    cells = [[_clean(c) for c in row] for row in rows]
    widths = [max([_dw(h)] + [_dw(r[i]) for r in cells]) for i, h in enumerate(headers)]
    over = sum(widths) + 2 * (len(widths) - 1) - _term_width()
    if over > 0:
        widths[flex] = max(12, widths[flex] - over)
    gap = "  "
    lines = [gap.join(_fit(h, w) for h, w in zip(headers, widths)).rstrip(),
             gap.join("─" * w for w in widths)]
    lines += [gap.join(_fit(c, w) for c, w in zip(r, widths)).rstrip() for r in cells]
    return lines


def _card(title: str, fields: list[tuple[str, object]], body: str | None = None,
          subtitle: str | None = None) -> list[str]:
    """Heading, aligned label/value rows, then wrapped body text."""
    lines = [title]
    if subtitle:
        lines.append(subtitle)
    shown = [(k, _clean(v)) for k, v in fields if _clean(v)]
    if shown:
        lines.append("")
        lw = max(_dw(k) for k, _ in shown)
        vw = max(20, _term_width() - lw - 2)
        for k, v in shown:
            wrapped = textwrap.wrap(v, vw) or [""]
            lines.append(f"{k}{' ' * (lw - _dw(k))}  {wrapped[0]}")
            lines += [" " * (lw + 2) + w for w in wrapped[1:]]
    if body:
        lines.append("")
        for para in str(body).replace("\r", "").split("\n"):
            lines += textwrap.wrap(para, min(_term_width(), 100)) or [""]
    return lines


_EMPTY = (None, "", "0000-00-00", "0001-01-01", [], {})


def _clean(v: object) -> str:
    if v in _EMPTY:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return ", ".join(_clean(x.get("name") if isinstance(x, dict) else x) for x in v)
    return " ".join(str(v).split())


def _num(v: object) -> str | None:
    """Thousands separators for counts (not for ids)."""
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) and v else None


def _pick(d: dict, *keys: str) -> object:
    for k in keys:
        v = d.get(k)
        if v not in _EMPTY:
            return v
    return None


def _items(data: object, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys + ("items", "results", "entries", "data", "titles"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _limited(rows: list, limit: int | None, default: int | None) -> tuple[list, str | None]:
    n = default if limit is None else limit
    if not n or len(rows) <= n:
        return rows, None
    return rows[:n], f"showing {n} of {len(rows)} · -n 0 shows all"


def _mdl_url(slug: object) -> str | None:
    if not slug:
        return None
    s = str(slug)
    return s if s.startswith("http") else "https://mydramalist.com/" + s.lstrip("/")


def _rank(v: object) -> str | None:
    """'#11,994'; MDL uses 99999 for 'not ranked'."""
    return f"#{v:,}" if isinstance(v, int) and 0 < v < 99999 else None


def _year(d: dict) -> str:
    y = _pick(d, "year", "released_year")
    if y:
        return str(y)
    date = _pick(d, "released", "aired_start", "release_date")
    return str(date)[:4] if date else ""


def _rating(d: dict) -> str:
    r = _pick(d, "rating", "score")
    try:
        return f"{float(r):.1f}" if r and float(r) > 0 else ""
    except (TypeError, ValueError):
        return ""


# -- renderers: data → list of lines ----------------------------------------
def _render_genres(data: object, args) -> list[str]:
    rows = [[g.get("id"), g.get("name")] for g in _items(data) if isinstance(g, dict)]
    return _table(["ID", "Genre"], rows) + ["", f"{len(rows)} genres"]


def _render_languages(data: object, args) -> list[str]:
    langs = _items(data, "languages")
    rows = [[x.get("code"), x.get("name"), x.get("native_name")] for x in langs
            if isinstance(x, dict)]
    return _table(["Code", "Language", "Native name"], rows) + ["", f"{len(rows)} languages"]


def _render_calendar(data: object, args) -> list[str]:
    rels = {r.get("id"): r for r in _items(data, "relationships") if isinstance(r, dict)}
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))  # local midnight
    end = start + args.days * 86400
    items = sorted((i for i in _items(data) if isinstance(i, dict)
                    and start <= (i.get("released_at") or 0) < end),
                   key=lambda i: (i["released_at"],
                                  str((rels.get(i.get("rid")) or {}).get("title") or ""),
                                  i.get("episode_number") or 0))
    if not items:
        return [f"Nothing scheduled in the next {args.days} day(s)."]
    out: list[str] = []
    day = None
    for it in items:
        lt = time.localtime(it["released_at"])
        if lt[:3] != day:
            day = lt[:3]
            if out:
                out.append("")
            out.append(time.strftime("%A %d %B %Y", lt))
        rel = rels.get(it.get("rid")) or rels.get(it.get("id")) or {}
        ep = it.get("episode_number")
        what = f"ep {ep}" if ep else (rel.get("type") or "")
        line = f"  {time.strftime('%H:%M', lt)}  {_fit(_clean(rel.get('title') or it.get('permalink')), 40)}" \
               f"  {_fit(what, 8)}  {_clean(rel.get('country'))}"
        out.append(line.rstrip())
    out += ["", f"{len(items)} releases in the next {args.days} day(s) (local time) · "
                f"--days N to change"]
    return out


def _title_table(items: list) -> list[list[object]]:
    return [[i.get("id"), _pick(i, "title", "name"), _year(i),
              _pick(i, "media_type", "type", "content_type"), _pick(i, "country", "language"),
              _rating(i)] for i in items]


def _render_search(data: object, args) -> list[str]:
    items = [i for i in _items(data) if isinstance(i, dict)]
    if not items:
        return ["No titles found."]
    rows, more = _limited(_title_table(items), args.limit, None)
    q = " ".join(args.query)
    out = _table(["ID", "Title", "Year", "Type", "Country", "Rating"], rows)
    out += ["", f"page {args.page} · details: mdlaw title <ID> · next page: "
                f"mdlaw search {shlex.quote(q)} --page {args.page + 1}"]
    return out + ([more] if more else [])


def _browse_cmd(args, page: int) -> str:
    parts = ["mdlaw browse"]
    parts += [f"--type {t}" for t in args.types or []]
    parts += [f"--genre {shlex.quote(g)}" for g in args.genres or []]
    parts += [f"--country {shlex.quote(c)}" for c in args.countries or []]
    if args.released_from:
        parts.append(f"--from {args.released_from}")
    if args.sort != "top":
        parts.append(f"--sort {args.sort}")
    return " ".join(parts + [f"--page {page}"])


def _render_browse(data: object, args) -> list[str]:
    items = [i for i in _items(data) if isinstance(i, dict)]
    if not items:
        return ["No titles match these filters." + (" Try an earlier page." if args.page > 1 else "")]
    rows, more = _limited(_title_table(items), args.limit, None)
    out = _table(["ID", "Title", "Year", "Type", "Country", "Rating"], rows)
    out += ["", f"page {args.page} · sorted by {args.sort} · details: mdlaw title <ID>",
            f"next page: {_browse_cmd(args, args.page + 1)}"]
    return out + ([more] if more else [])


def _render_recommendations(data: object, args) -> list[str]:
    recs = [r for r in _items(data) if isinstance(r, dict)]
    if not recs:
        return ["No recommendations."]
    recs, more = _limited(recs, args.limit, 10)
    width = min(_term_width(), 100)
    out: list[str] = []
    for r in recs:
        a = r.get("title") if isinstance(r.get("title"), dict) else {}
        b = r.get("similar") if isinstance(r.get("similar"), dict) else {}

        def name(t: dict) -> str:
            y = _year(t)
            return f"{_clean(t.get('title'))}" + (f" ({y})" if y else "") + f" [{t.get('id')}]"
        out.append(f"Liked {name(a)}")
        out.append(f"  → try {name(b)}")
        reason = _clean(r.get("content"))
        if reason:
            out += textwrap.wrap(f'"{reason}"', width, initial_indent="    ",
                                 subsequent_indent="     ")[:4]
        who = _clean((r.get("author") or {}).get("display_name"))
        likes = _num(r.get("total_likes"))
        meta = " · ".join(x for x in (f"by {who}" if who else "", f"{likes} likes" if likes else "") if x)
        if meta:
            out.append(f"    {meta}")
        out.append("")
    out.append(f"page {args.page} · details: mdlaw title <ID> · next: "
               f"mdlaw recommendations --page {args.page + 1}")
    return out + ([more] if more else [])


def _render_lists(data: object, args) -> list[str]:
    items = [i for i in _items(data, "lists") if isinstance(i, dict)]
    if not items:
        return ["No lists."]
    rows = [[i.get("id"), _pick(i, "name", "title"), _num(i.get("count")) or _clean(i.get("count")),
             _num(i.get("total_likes")), _num(i.get("total_views"))] for i in items]
    rows, more = _limited(rows, args.limit, None)
    out = _table(["ID", "List", "Titles", "Likes", "Views"], rows)
    return out + ["", f"{args.which} lists · -n N for more"] + ([more] if more else [])


def _render_search_people(data: object, args) -> list[str]:
    items = [i for i in _items(data) if isinstance(i, dict)]
    if not items:
        return ["No people found."]
    rows = [[i.get("id"), _pick(i, "name", "display_name"),
             _pick(i, "original_name", "native_name"), _pick(i, "nationality", "country")]
            for i in items]
    rows, more = _limited(rows, args.limit, None)
    q = " ".join(args.query)
    out = _table(["ID", "Name", "Native name", "Nationality"], rows)
    out += ["", f"page {args.page} · profile: mdlaw people <ID> · next page: "
                f"mdlaw search-people {shlex.quote(q)} --page {args.page + 1}"]
    return out + ([more] if more else [])


def _render_title(data: object, args) -> list[str]:
    if not isinstance(data, dict):
        return _render_generic(data, args)
    name = _clean(_pick(data, "title", "name")) or f"title {args.id}"
    year = _year(data)
    genres = data.get("genres")
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",")]
    rating = _rating(data)
    votes = _pick(data, "votes", "rating_count")
    eps, runtime = data.get("episodes"), data.get("runtime")
    aired = _pick(data, "release_dates_fmt", "aired")
    if not aired and data.get("aired_start"):
        aired = " – ".join(str(x) for x in (data.get("aired_start"), data.get("aired_end")) if x)
    alt = data.get("alt_titles")
    fields = [
        ("Native title", _pick(data, "original_title", "native_title")),
        ("Also known as", ", ".join(map(str, alt[:3])) + (" …" if len(alt) > 3 else "")
         if isinstance(alt, list) and alt else None),
        ("Type", _pick(data, "media_type", "type", "content_type")),
        ("Country", data.get("country")),
        ("Episodes", (f"{eps}" + (f" × {runtime} min" if runtime else "")) if eps else None),
        ("Aired", aired),
        ("Status", data.get("status")),
        ("Rating", f"{rating}/10" + (f" ({_num(votes) or votes} votes)" if votes else "")
         if rating else None),
        ("Ranked", _rank(_pick(data, "ranked", "ranking"))),
        ("Popularity", _rank(data.get("popularity"))),
        ("Watchers", _num(data.get("watchers"))),
        ("Certification", data.get("certification")),
        ("Genres", genres),
        ("Tags", data.get("tags")),
        ("Link", _mdl_url(_pick(data, "permalink", "url", "slug"))),
    ]
    return _card(f"{name} ({year})" if year else name, fields,
                 body=_pick(data, "synopsis", "description"))


def _render_people(data: object, args) -> list[str]:
    if not isinstance(data, dict):
        return _render_generic(data, args)
    name = _clean(_pick(data, "name", "display_name")) or f"person {args.id}"
    native = _clean(data.get("original_name"))
    fields = [
        ("Nationality", data.get("nationality")),
        ("Gender", data.get("gender")),
        ("Born", data.get("birthday")),
        ("Died", data.get("dod")),
        ("Flowers", _num(data.get("flowers"))),
        ("Rank", f"#{data['ranked']}" if data.get("ranked") else None),
        ("Link", _mdl_url(data.get("slug")) or _mdl_url(data.get("permalink"))),
    ]
    return _card(f"{name} ({native})" if native else name, fields,
                 body=data.get("biography"))


def _render_leaderboard(data: object, args) -> list[str]:
    entries = [e for e in _items(data, "entries") if isinstance(e, dict)]
    rows = []
    for e in entries:
        now, prev = e.get("ranked"), e.get("previous_rank")
        move = ""
        if isinstance(now, int) and isinstance(prev, int) and prev:
            move = f"▲{prev - now}" if prev > now else f"▼{now - prev}" if now > prev else "="
        rows.append([now, move, e.get("display_name"), e.get("nationality"),
                     _num(e.get("total_flowers")), e.get("id")])
    rows, more = _limited(rows, args.limit, 20)
    out = _table(["#", "Move", "Name", "Nationality", "Flowers", "ID"], rows, flex=2)
    out += ["", f"{args.period} leaderboard · profile: mdlaw people <ID>"]
    return out + ([more] if more else [])


def _render_profile(data: object, args) -> list[str]:
    if not isinstance(data, dict):
        return _render_generic(data, args)
    name = _clean(_pick(data, "display_name", "name", "username")) or "profile"
    user = _clean(data.get("username"))
    fields = [
        ("Username", user if user != name else None),
        ("Location", data.get("location")),
        ("Gender", data.get("gender")),
        ("Joined", str(data.get("joined_at") or "")[:10]),
        ("Followers", _num(data.get("followers"))),
        ("Following", _num(data.get("following"))),
        ("Points", _num(data.get("points"))),
        ("Contributions", _num(data.get("contributions_points"))),
        ("Roles", [r.get("name") for r in data.get("roles") or [] if isinstance(r, dict)]),
        ("VIP", "yes" if data.get("vip") else None),
    ]
    if not any(_clean(v) for _, v in fields):
        return _render_generic(data, args)
    return _card(name, fields, body=data.get("about"))


# list_id → status, verified against the /sync/mylist/<status> endpoints.
_LIST_STATUS = {1: "watching", 2: "completed", 3: "plan to watch", 4: "on hold",
                5: "dropped", 6: "not interested"}


def _render_watchlist(data: object, args) -> list[str]:
    items = [i for i in _items(data, "list", "watchlist") if isinstance(i, dict)]
    if not items:
        return [f"Nothing in your {args.status or 'watching'} list."]
    rows = []
    for i in items:
        t = i.get("title") if isinstance(i.get("title"), dict) else i
        seen, total = i.get("episode_seen"), t.get("episodes")
        progress = f"{seen}/{total}" if isinstance(seen, int) and total else _clean(seen)
        score = i.get("rating")
        rows.append([t.get("id"), _pick(t, "title", "name"), _year(t),
                     _LIST_STATUS.get(i.get("list_id"), _clean(_pick(i, "status"))),
                     progress, f"{score:g}" if isinstance(score, (int, float)) and score else ""])
    show_status = len({r[3] for r in rows}) > 1
    headers = ["ID", "Title", "Year", "Status", "Progress", "Score"]
    if not show_status:  # one list → the status column is redundant
        headers.pop(3)
        rows = [r[:3] + r[4:] for r in rows]
    rows, more = _limited(rows, args.limit, 50)
    label = args.status or "watching"
    out = _table(headers, rows)
    out += ["", f"{len(items)} titles in your {label} list · details: mdlaw title <ID>"]
    if not args.status:
        out.append("other lists: mdlaw watchlist completed|plantowatch|onhold|dropped|notinterested")
    return out + ([more] if more else [])


_GENERIC_COLS = (("ID", ("id", "rid")), ("Name", ("title", "name", "display_name", "username")),
                 ("Year", ("year",)), ("Type", ("media_type", "type")),
                 ("Country", ("country", "nationality")), ("Status", ("status",)),
                 ("Rating", ("rating", "score")))


def _render_generic(data: object, args) -> list[str]:
    """Fallback for shapes without a dedicated renderer: a table for lists of
    objects, label/value rows for an object (skipping URLs and nested data)."""
    items = _items(data)
    if items and all(isinstance(i, dict) for i in items):
        cols = [(h, ks) for h, ks in _GENERIC_COLS if any(_pick(i, *ks) for i in items)]
        if cols:
            rows = [[_pick(i, *ks) for _, ks in cols] for i in items]
            rows, more = _limited(rows, getattr(args, "limit", None), None)
            flex = next((n for n, (h, _) in enumerate(cols) if h == "Name"), 0)
            return _table([h for h, _ in cols], rows, flex=flex) + ([more] if more else [])
    if isinstance(data, dict):
        fields = [(k.replace("_", " ").capitalize(), v) for k, v in data.items()
                  if not isinstance(v, (dict, list)) and not str(v).startswith("http")]
        return _card("", fields)[2:] or ["(no displayable fields — try --json)"]
    if isinstance(data, list):
        return [_clean(x) for x in data] or ["No results."]
    return [_clean(data)]


_RENDERERS = {
    "genres": _render_genres, "languages": _render_languages,
    "calendar": _render_calendar, "search": _render_search,
    "search-people": _render_search_people, "title": _render_title,
    "people": _render_people, "watchlist": _render_watchlist,
    "browse": _render_browse, "lists": _render_lists,
    "recommendations": _render_recommendations, "recs": _render_recommendations,
    "me": _render_profile, "leaderboard": _render_leaderboard,
}


# -- errors -----------------------------------------------------------------
def _explain(e: HTTPException) -> tuple[str, str | None]:
    """HTTPException → (one-line message, hint with the next step)."""
    detail = e.detail
    msg = detail.get("detail", detail) if isinstance(detail, dict) else detail
    if isinstance(msg, dict):
        msg = msg.get("message") or msg.get("error_description") or msg.get("error") or \
            json.dumps(msg, ensure_ascii=False)
    msg = " ".join(str(msg).split())
    code = e.status_code
    if code == 400 and "MDL_USERNAME" in msg and "not set" not in msg:
        return ("this command needs a MyDramaList account",
                "log in once with `mdlaw auth` (or set MDL_USERNAME and MDL_PASSWORD)")
    if code == 400 and "not set" in msg:
        return ("no MyDramaList account configured",
                "log in with `mdlaw auth`, or set MDL_USERNAME and MDL_PASSWORD")
    if code == 401:
        return (msg.split(" — ")[0] if "mdlaw auth" in msg else f"not authorized: {msg}",
                "log in again with `mdlaw auth`")
    if code == 403 and "Cloudflare" in msg:
        return ("Cloudflare blocked the request (your IP looks like a bot to it)",
                "retry with `--transport curl_cffi`, or from another network")
    if code == 404:
        return "not found on MyDramaList (HTTP 404)", "check the id — ids come from search results"
    if code == 428:
        return "this account has two-factor authentication enabled", \
            "mdlaw can't complete 2FA logins; disable 2FA or use an app password"
    if code == 429:
        return "MyDramaList is rate-limiting requests (HTTP 429)", "wait a minute and try again"
    if code == 504:
        return "MyDramaList did not respond in time", "try again; -v shows the request timings"
    if code == 502:
        return f"could not reach MyDramaList: {msg}", "check your connection and try again"
    if code >= 500:
        return f"MyDramaList returned an error (HTTP {code})", "try again later"
    return f"{msg} (HTTP {code})", None


def _print_error(message: str, hint: str | None = None) -> None:
    print(f"error: {message}", file=sys.stderr)
    if hint:
        print(f"hint:  {hint}", file=sys.stderr)


# -- logging ----------------------------------------------------------------
class _StderrHandler(logging.StreamHandler):
    """Writes to the *current* sys.stderr (survives stream swaps in tests)."""

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


class _CLIFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"warning: {msg}"
        return f"  · {msg}"


def _setup_cli_logging(verbosity: int) -> None:
    h = _StderrHandler()
    h.setFormatter(_CLIFormatter())
    log.handlers[:] = [h]
    log.setLevel(logging.WARNING if verbosity <= 0 else
                 logging.INFO if verbosity == 1 else logging.DEBUG)
    log.propagate = False


class _Spinner:
    """'Contacting MyDramaList…' on stderr when a call takes a moment. Only on
    an interactive terminal and when -v isn't printing its own progress."""

    def __init__(self, enabled: bool, text: str = "Contacting MyDramaList") -> None:
        self.enabled, self.text = enabled, text
        self._task: asyncio.Task | None = None
        self._shown = False

    async def _run(self) -> None:
        await asyncio.sleep(0.6)
        utf = (getattr(sys.stderr, "encoding", "") or "").lower().replace("-", "") == "utf8"
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if utf else "|/-\\"
        i = 0
        while True:
            self._shown = True
            sys.stderr.write(f"\r{frames[i % len(frames)]} {self.text}…")
            sys.stderr.flush()
            i += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> "_Spinner":
        if self.enabled:
            self._task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            if self._shown:
                sys.stderr.write("\r" + " " * (_dw(self.text) + 4) + "\r")
                sys.stderr.flush()


# -- commands ---------------------------------------------------------------
async def _call(args) -> object:
    mdl = MDL()
    try:
        c = args.command
        if c == "genres":
            return await mdl.genres()
        if c == "languages":
            return await mdl.languages()
        if c == "calendar":
            return await mdl.calendar()
        if c == "search":
            return await mdl.search(" ".join(args.query), country=args.country,
                                    type=args.type, year=args.year, page=args.page)
        if c == "search-people":
            return await mdl.search_people(" ".join(args.query), page=args.page)
        if c == "browse":
            return await mdl.browse(types=args.types, genres=args.genres,
                                    countries=args.countries, released_from=args.released_from,
                                    sort=args.sort, page=args.page)
        if c in ("recommendations", "recs"):
            return await mdl.recommendations(page=args.page)
        if c == "lists":
            n = max(args.limit or 10, 1)
            return await {"trending": mdl.lists_trending, "recent": mdl.lists_recent,
                          "popular": mdl.lists_popular, "featured": mdl.lists_featured}[args.which](n)
        if c == "title":
            return await mdl.title(args.id)
        if c == "people":
            return await mdl.people(args.id)
        if c == "watchlist":
            return await mdl.watchlist(args.status)
        if c == "me":
            return await mdl.me()
        if c == "leaderboard":
            return await mdl.leaderboard(args.period)
        raise _CLIError(f"unknown command: {c}", "see `mdlaw --help`", code=2)
    finally:
        await mdl.close()


def _want_json(args) -> bool:
    if args.output == "json":
        return True
    if args.output == "text":
        return False
    return not sys.stdout.isatty()


def _emit(data: object, args) -> None:
    if _want_json(args):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    render = _RENDERERS.get(args.command, _render_generic)
    print("\n".join(render(data, args)))


def _run_cli(argv: list[str]) -> int:
    """Parse arguments, run one command, print the result. Returns the exit code."""
    _ensure_utf8_stdio()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse: --help/--version (0) or bad usage (2)
        return e.code if isinstance(e.code, int) else 2
    if args.command is None:
        parser.print_help()
        return 0
    args.limit = getattr(args, "limit", None)
    if getattr(args, "transport", None):
        client.TRANSPORT = args.transport
        os.environ["MDL_TRANSPORT"] = args.transport  # serve re-imports the module
    if args.command in ("serve", "run"):
        # The server sets up its own log format (MDL_LOG_LEVEL, default INFO);
        # -v here means DEBUG.
        if args.verbose:
            os.environ["MDL_LOG_LEVEL"] = "DEBUG"
        extra = [x for k in ("host", "port") if getattr(args, k)
                 for x in (f"--{k}", getattr(args, k))]
        return _serve(extra)
    _setup_cli_logging(args.verbose)
    try:
        if args.command == "self":
            return self_check()
        if args.command == "auth":
            if args.username == "status" and not args.password:
                return _cli_auth_status(args)
            return _cli_auth(args)
        if args.command == "logout":
            return _cli_logout()
        if not client.auth_enabled():
            client._load_auth()
        spin = sys.stderr.isatty() and args.verbose == 0 and not _want_json(args)

        async def run() -> object:
            async with _Spinner(spin):
                return await _call(args)
        _emit(asyncio.run(run()), args)
        return 0
    except HTTPException as e:
        _print_error(*_explain(e))
        return 1
    except _CLIError as e:
        _print_error(e.message, e.hint)
        return e.code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:  # unexpected: short message, traceback only with -vv
        if args.verbose >= 2:
            import traceback
            traceback.print_exc()
        _print_error(f"unexpected {type(e).__name__}: {e}",
                     "run again with -vv for the full traceback")
        return 1


def _cli_auth(args) -> int:
    """mdlaw auth [username] [password] — log in and save the session."""
    username = args.username or os.environ.get("MDL_USERNAME", "").strip() or None
    password = args.password
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    password = password or os.environ.get("MDL_PASSWORD", "").strip() or None
    interactive = sys.stdin.isatty()
    if not username:
        if not interactive:
            raise _CLIError("no username given", "usage: mdlaw auth <username>", code=2)
        username = input("MDL username or email: ").strip()
    if not username:
        raise _CLIError("a username is required", code=2)
    if not password:
        if not interactive:
            raise _CLIError("no password given",
                            "pipe it with --password-stdin or set MDL_PASSWORD", code=2)
        import getpass
        password = getpass.getpass("Password: ")
    client.MDL_USERNAME, client.MDL_PASSWORD, client._PERSIST_AUTH = username, password, True

    async def do_login() -> None:
        try:
            async with _Spinner(sys.stderr.isatty() and args.verbose == 0, "Logging in"):
                await client.login(force=True)
        finally:
            await client.close_client()
    asyncio.run(do_login())
    left = _fmt_duration(_auth["expires_at"] - time.time())
    if _want_json(args):
        print(json.dumps(_session_info(), indent=2, ensure_ascii=False))
    else:
        print(f"Logged in as {username}.")
        print(f"Session saved to {client._AUTH_FILE} (token valid for {left}, refreshed automatically).")
    return 0


def _session_info() -> dict:
    """Saved-session summary for `auth status` — never includes tokens."""
    user = _auth["user"] if isinstance(_auth["user"], dict) else {}
    now = time.time()
    return {
        "logged_in": bool(_auth["token"] or _auth["refresh_token"]),
        "account": user.get("name") or user.get("username") or _auth.get("username") or None,
        "expires_at": _auth["expires_at"] or None,
        "expired": bool(_auth["token"]) and _auth["expires_at"] <= now,
        "can_refresh": bool(_auth["refresh_token"]),
        "session_file": client._AUTH_FILE,
        "env_credentials": bool(os.environ.get("MDL_USERNAME") and os.environ.get("MDL_PASSWORD")),
    }


def _cli_auth_status(args) -> int:
    """Show the saved session: account, token state, what happens next."""
    client._load_auth()
    info = _session_info()
    if _want_json(args):
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0
    env = "MDL_USERNAME / MDL_PASSWORD are set (used when the session can't be refreshed)" \
        if info["env_credentials"] else None
    if not info["logged_in"]:
        print("Not logged in.")
        if env:
            print(env)
        print("Log in with: mdlaw auth <username>")
        return 0
    left = _auth["expires_at"] - time.time()
    if not info["expired"]:
        token = f"valid for {_fmt_duration(left)}"
    elif info["can_refresh"]:
        token = f"expired {_fmt_duration(left)} ago — the next request will try to refresh it"
    else:
        token = f"expired {_fmt_duration(left)} ago — log in again with `mdlaw auth`"
    print("\n".join(_card("Saved session", [
        ("Account", info["account"] or "(unknown — log in again to record it)"),
        ("Token", token),
        ("Device id", _auth["device_id"]),
        ("File", info["session_file"]),
        ("Env", env),
    ])))
    return 0


def _cli_logout() -> int:
    """Remove the saved session."""
    try:
        os.remove(client._AUTH_FILE)
        print(f"Logged out (removed {client._AUTH_FILE}).")
    except FileNotFoundError:
        print("Not logged in — no saved session to remove.")
    except OSError as e:
        raise _CLIError(f"could not remove {client._AUTH_FILE}: {e}") from None
    return 0


def _parse_serve_args(args: list[str]) -> tuple[str, int]:
    """--host/--port (also --host=X); defaults from HOST/PORT env, else
    0.0.0.0:8000. Raises ValueError on bad input."""
    host = os.environ.get("HOST", "").strip() or "0.0.0.0"
    port_s = os.environ.get("PORT", "").strip() or "8000"
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if name not in ("--host", "--port"):
            raise ValueError(f"unknown serve option: {a}")
        if not eq:
            if i + 1 >= len(args):
                raise ValueError(f"{name} requires a value")
            i += 1
            val = args[i]
        if name == "--host":
            host = val
        else:
            port_s = val
        i += 1
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"invalid port: {port_s}") from None
    if not 0 < port < 65536:
        raise ValueError(f"invalid port: {port}")
    return host, port


def _serve(args: list[str]) -> int:
    try:
        host, port = _parse_serve_args(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    import uvicorn
    uvicorn.run("mdlaw:app", host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (console script `mdlaw`). No command → start the server
    (as do bare --host/--port, kept for compatibility)."""
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0].partition("=")[0] in ("--host", "--port"):
        return _serve(args)
    return _run_cli(args)
