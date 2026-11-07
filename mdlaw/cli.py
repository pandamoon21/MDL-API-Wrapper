"""Command-line interface: offline self-check, argparse parser, auth and
``main`` (console script ``mdlaw``, ``python -m mdlaw``). Text output
(tables, cards, renderers, error messages) lives in ``render``."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from fastapi import HTTPException

from . import caching, client
from ._config import API_KEY, __version__, default_headers, log
from .caching import SQLCache, TTLCache
from .client import BROWSE_COUNTRIES, BROWSE_SORTS, BROWSE_TYPES, _auth, _filter_ids, _fmt_duration, _md5
from .library import MDL
from .render import _RENDERERS, _card, _dw, _explain, _render_generic
from .render import _fit, _table  # noqa: F401  (re-exported: tests use mdlaw.cli._fit/_table)

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


# -- errors -----------------------------------------------------------------
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
