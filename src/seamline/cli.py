"""The `seamline` command line. Commands from later phases print a placeholder."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from seamline import __version__
from seamline.config import ConfigError
from seamline.extract.providers import ProviderError, make_provider
from seamline.ledger.db import open_ledger
from seamline.ledger_views import run_ingest, run_scan, show_drift, show_facts
from seamline.project_init import InitError, run_init
from seamline.session_views import resolve_project, run_extract, show_session, show_sessions

# name -> (phase it arrives in, help text)
PLANNED: dict[str, tuple[int, str]] = {
    "brief": (4, "Print exactly what a session would receive"),
    "worker": (4, "Run the background worker (normally started by hooks)"),
    "hook": (4, "Entry point for Claude Code hooks"),
    "pause": (4, "Stop recording this project (removes its hooks; keeps config and ledger)"),
    "resume": (4, "Start recording again (re-adds hooks, including for new services)"),
    "remove": (4, "Remove hooks and seamline.toml; asks before deleting .seamline/"),
    "status": (4, "Health check: recent hooks, worker state, pending sessions"),
    "mcp": (5, "Start the MCP server (stdio)"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seamline",
        description="Shared, cross-service session memory for Claude Code.",
    )
    parser.add_argument("--version", action="version", version=f"seamline {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    init = sub.add_parser("init", help="Detect services and contracts; write seamline.toml")
    init.add_argument("path", nargs="?", default=".", help="Project root (default: current folder)")
    init.add_argument("-y", "--yes", action="store_true", help="Accept everything detected")
    init.add_argument("--force", action="store_true", help="Overwrite an existing seamline.toml")
    init.set_defaults(func=cmd_init)

    root_help = "Project folder (default: the project containing the current folder)"
    sessions = sub.add_parser("sessions", help="List the project's sessions by service")
    sessions.add_argument("--root", help=root_help)
    sessions.set_defaults(func=cmd_sessions)

    show = sub.add_parser("show", help="Print a session's events labeled keep/anchor/evidence/skip")
    show.add_argument("session", help="Session id (a unique prefix is enough)")
    show.add_argument("--root", help=root_help)
    show.add_argument("--all", action="store_true", help="Include skipped events")
    show.add_argument("--full", action="store_true", help="Print full text, not one line each")
    show.set_defaults(func=cmd_show)

    ext = sub.add_parser("extract", help="Extract facts from a session and print them")
    ext.add_argument("session", help="Session id (a unique prefix is enough)")
    ext.add_argument("--root", help=root_help)
    ext.add_argument(
        "--dry-run",
        action="store_true",
        help="Accepted for compatibility: extract never stores (use `seamline ingest`)",
    )
    ext.add_argument("--provider", help="Override [extract] provider")
    ext.add_argument("--model", help="Override [extract] model")
    ext.add_argument("--max-chunks", type=int, help="Only send the first N excerpts")
    ext.add_argument("--json", dest="json_path", help="Also write the full result to this file")
    ext.add_argument(
        "--structured",
        choices=("tool", "schema"),
        default="tool",
        help="How the model returns facts: a strict tool call (default) or structured outputs",
    )
    ext.add_argument("--debug", action="store_true", help="Print the model's raw reply to stderr")
    ext.set_defaults(func=cmd_extract)

    ing = sub.add_parser("ingest", help="Extract facts from new session lines into the ledger")
    ing.add_argument("session", nargs="?", help="Only this session (default: all with new lines)")
    ing.add_argument("--root", help=root_help)
    ing.add_argument("--all", action="store_true", help="Same as the default: every session")
    ing.add_argument(
        "--redo",
        action="store_true",
        help="Re-extract the given session from the start with the current prompt and rules",
    )
    ing.add_argument("-y", "--yes", action="store_true", help="Don't ask before calling the model")
    ing.add_argument("--max-chunks", type=int, help="Only send the first N excerpts per session")
    ing.add_argument("--model", help="Override [extract] model")
    ing.set_defaults(func=cmd_ingest)

    facts = sub.add_parser("facts", help="List ledger facts with their sources")
    facts.add_argument("--root", help=root_help)
    facts.add_argument("--service", help="Limit to one service")
    facts.add_argument(
        "--kind", choices=("provides", "consumes", "assumes", "decision", "dead_end")
    )
    facts.add_argument("--code", action="store_true", help="Only facts scanned from code")
    facts.add_argument("--all", action="store_true", help="Include superseded and stale facts")
    facts.set_defaults(func=cmd_facts)

    scan = sub.add_parser("scan", help="Re-read the [contracts] files into code facts")
    scan.add_argument("--root", help=root_help)
    scan.set_defaults(func=cmd_scan)

    drift = sub.add_parser("drift", help="List open contract mismatches with both quotes")
    drift.add_argument("--root", help=root_help)
    drift.set_defaults(func=cmd_drift)

    for name, (phase, text) in PLANNED.items():
        p = sub.add_parser(name, help=f"{text} [phase {phase}]")
        _add_planned_args(name, p)
        p.set_defaults(func=cmd_not_yet, phase=phase)

    return parser


def _add_planned_args(name: str, p: argparse.ArgumentParser) -> None:
    """Accept the arguments each future command will take, so their --help is already useful."""
    if name == "brief":
        p.add_argument("--service", help="Limit to one service")
    if name == "hook":
        p.add_argument("event", help="Hook event name, e.g. SessionStart")


def cmd_init(args: argparse.Namespace) -> int:
    try:
        run_init(Path(args.path), yes=args.yes, force=args.force)
    except (InitError, ConfigError) as e:
        print(f"seamline init: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nseamline init: cancelled, nothing written", file=sys.stderr)
        return 130
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    try:
        return show_sessions(resolve_project(args.root))
    except ConfigError as e:
        print(f"seamline sessions: {e}", file=sys.stderr)
        return 1


def cmd_show(args: argparse.Namespace) -> int:
    """Outside a project (and without --root), search every Claude Code session."""
    try:
        config = resolve_project(args.root)
    except ConfigError:
        config = None
    try:
        return show_session(config, args.session, show_all=args.all, full=args.full)
    except LookupError as e:
        print(f"seamline show: {e}", file=sys.stderr)
        return 1


def cmd_extract(args: argparse.Namespace) -> int:
    try:
        config = resolve_project(args.root)
        provider = make_provider(
            config, args.provider, args.model, structured=args.structured, debug=args.debug
        )
        report = run_extract(
            config, args.session, provider, max_chunks=args.max_chunks, json_path=args.json_path
        )
    except (ConfigError, LookupError, ProviderError) as e:
        print(f"seamline extract: {e}", file=sys.stderr)
        return 1
    return 1 if report.errors and not report.chunks_done else 0


def _ledger(args: argparse.Namespace, command: str):
    """Ledger commands need a real project: they write .seamline/ledger.db inside it."""
    config = resolve_project(args.root)
    if not config.path.exists():
        raise ConfigError(
            f"{config.root} has no seamline.toml; run `seamline init` there before `{command}`"
        )
    return config, open_ledger(config.root)


def cmd_ingest(args: argparse.Namespace) -> int:
    try:
        config, conn = _ledger(args, "ingest")
        if args.redo and not args.session:
            raise ConfigError("--redo needs a session id: `seamline ingest <session> --redo`")
        return run_ingest(
            conn,
            config,
            lambda: make_provider(config, model=args.model),
            session_id=args.session,
            redo=args.redo,
            model=args.model,
            yes=args.yes,
            max_chunks=args.max_chunks,
        )
    except (ConfigError, LookupError, ProviderError) as e:
        print(f"seamline ingest: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nseamline ingest: interrupted; finished sessions are saved", file=sys.stderr)
        return 130


def cmd_facts(args: argparse.Namespace) -> int:
    try:
        _, conn = _ledger(args, "facts")
    except ConfigError as e:
        print(f"seamline facts: {e}", file=sys.stderr)
        return 1
    return show_facts(
        conn,
        service=args.service,
        kind=args.kind,
        include_inactive=args.all,
        origin="code" if args.code else None,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    try:
        config, conn = _ledger(args, "scan")
    except ConfigError as e:
        print(f"seamline scan: {e}", file=sys.stderr)
        return 1
    return run_scan(conn, config)


def cmd_drift(args: argparse.Namespace) -> int:
    try:
        config, conn = _ledger(args, "drift")
    except ConfigError as e:
        print(f"seamline drift: {e}", file=sys.stderr)
        return 1
    return show_drift(conn, config)


def cmd_not_yet(args: argparse.Namespace) -> int:
    print(f"seamline {args.command}: not yet implemented (phase {args.phase})", file=sys.stderr)
    return 1  # Not 2: Claude Code treats a hook's exit code 2 as a blocking error


def main(argv: list[str] | None = None) -> int:
    # Exit quietly when output is piped into `head` and the pipe closes.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
