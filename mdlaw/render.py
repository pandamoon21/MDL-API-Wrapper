"""Text output for the CLI: display-width aware tables and cards, the
per-command renderers (API data -> list of lines) and HTTP error messages.

Everything here returns strings; nothing prints. The only environment
inputs are the terminal width (``_term_width``) and, for the calendar, the
local clock."""
from __future__ import annotations

import json
import shlex
import shutil
import textwrap
import time
import unicodedata

from fastapi import HTTPException


# -- layout helpers ---------------------------------------------------------
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
