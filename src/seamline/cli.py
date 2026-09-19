"""The `seamline` command line. Commands from later phases print a placeholder."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from seamline import __version__
from seamline.config import ConfigError
from seamline.extract.providers import ProviderError, make_provider
from seamline.hooks.settings import SettingsError
from seamline.ledger.db import open_ledger
from seamline.ledger_views import run_ingest, run_scan, show_drift, show_facts
from seamline.lifecycle import (
    install_hooks,
    run_brief,
    run_pause,
    run_remove,
    run_resume,
    run_status,
)
from seamline.mcp_server.registration import McpConfigError
from seamline.project_init import InitError, run_init
from seamline.session_views import resolve_project, run_extract, show_session, show_sessions

# name -> (phase it arrives in, help text); empty now that every planned command exists
PLANNED: dict[str, tuple[int, str]] = {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seamline",
        description="Shared, cross-service session memory for Claude Code.",
    )
    parser.add_argument("--version", action="version", version=f"seamline {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    install = sub.add_parser(
        "install",
        help="One-time setup: track sessions in any folder of any Seamline project "
        "(user-level hooks that do nothing outside projects)",
    )
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser(
        "uninstall-global", help="Undo `seamline install` (projects and ledgers are kept)"
    )
    uninstall.set_defaults(func=cmd_uninstall_global)

    init = sub.add_parser("init", help="Detect services and contracts; write seamline.toml")
    init.add_argument("path", nargs="?", default=".", help="Project root (default: current folder)")
    init.add_argument("-y", "--yes", action="store_true", help="Accept everything detected")
    init.add_argument("--force", action="store_true", help="Overwrite an existing seamline.toml")
    init.add_argument(
        "--no-hooks",
        action="store_true",
        help="Don't install hooks (add them later with `seamline resume`)",
    )
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

    brief = sub.add_parser("brief", help="Print exactly what a new session would receive")
    brief.add_argument("--root", help=root_help)
    brief.add_argument("--service", help="As a session in this service (default: project root)")
    brief.set_defaults(func=cmd_brief)

    status = sub.add_parser("status", help="Health check: hooks, worker, flagged sessions, spend")
    status.add_argument("--root", help=root_help)
    status.set_defaults(func=cmd_status)

    for name, text in (
        ("pause", "Stop recording this project (removes its hooks; keeps config and ledger)"),
        ("resume", "Install hooks / start recording again (picks up new services)"),
        ("remove", "Remove hooks and seamline.toml; asks before deleting .seamline/"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--root", help=root_help)
        p.set_defaults(func=cmd_lifecycle)

    worker = sub.add_parser("worker", help="Run the background worker (normally started by hooks)")
    worker.add_argument("--root", help=root_help)
    worker.set_defaults(func=cmd_worker)

    mcp = sub.add_parser("mcp", help="Run the MCP server over stdio (Claude Code starts it)")
    mcp.add_argument("--root", help=root_help)
    mcp.set_defaults(func=cmd_mcp)

    hook = sub.add_parser("hook", help="Entry point for Claude Code hooks (JSON on stdin)")
    hook.add_argument("event", help="Hook event name, e.g. SessionStart")
    hook.set_defaults(func=cmd_hook)

    for name, (phase, text) in PLANNED.items():
        p = sub.add_parser(name, help=f"{text} [phase {phase}]")
        _add_planned_args(name, p)
        p.set_defaults(func=cmd_not_yet, phase=phase)

    return parser


def _add_planned_args(name: str, p: argparse.ArgumentParser) -> None:
    """Accept the arguments each future command will take, so their --help is already useful."""


def cmd_init(args: argparse.Namespace) -> int:
    try:
        result = run_init(Path(args.path), yes=args.yes, force=args.force)
        if not args.no_hooks:
            print("\nRecording (this project only; `seamline pause` turns it off):")
            install_hooks(result.config, approve_mcp=args.yes or _ask_approval())
    except (InitError, ConfigError, SettingsError, McpConfigError) as e:
        print(f"seamline init: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nseamline init: cancelled, nothing written", file=sys.stderr)
        return 130
    return 0


def _ask_approval() -> bool:
    """Default yes; no answer (end of input) means no, so nothing is approved unasked."""
    try:
        answer = input(
            "Turn on Seamline's MCP tools in this project's Claude sessions (registers and "
            "approves the seamline server in .mcp.json)? [Y/n] "
        )
    except EOFError:
        print()
        return False
    return answer.strip().lower() in ("", "y", "yes")


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


def cmd_brief(args: argparse.Namespace) -> int:
    try:
        config, conn = _ledger(args, "brief")
    except ConfigError as e:
        print(f"seamline brief: {e}", file=sys.stderr)
        return 1
    return run_brief(conn, config, args.service)


def cmd_status(args: argparse.Namespace) -> int:
    try:
        config, conn = _ledger(args, "status")
    except ConfigError as e:
        print(f"seamline status: {e}", file=sys.stderr)
        return 1
    return run_status(conn, config)


def cmd_lifecycle(args: argparse.Namespace) -> int:
    try:
        config = resolve_project(args.root)
        if not config.path.exists():
            raise ConfigError(f"{config.root} has no seamline.toml; nothing to {args.command}")
        if args.command == "pause":
            return run_pause(config)
        if args.command == "resume":
            return run_resume(config)
        return run_remove(config)
    except (ConfigError, SettingsError, McpConfigError) as e:
        print(f"seamline {args.command}: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(f"\nseamline {args.command}: cancelled", file=sys.stderr)
        return 130


def cmd_worker(args: argparse.Namespace) -> int:
    from seamline.worker import run_worker, setup_logging

    try:
        config = resolve_project(args.root)
    except ConfigError as e:
        print(f"seamline worker: {e}", file=sys.stderr)
        return 1
    setup_logging(config.root)
    return run_worker(config, lambda: make_provider(config))


def cmd_mcp(args: argparse.Namespace) -> int:
    """With --root, that project. Without it, the project containing the current folder,
    or a server with no tools outside any project."""
    from seamline.config import find_project
    from seamline.mcp_server.server import run, run_outside_project

    try:
        config = resolve_project(args.root) if args.root else find_project(Path.cwd())
    except ConfigError as e:
        print(f"seamline mcp: {e}", file=sys.stderr)
        return 1
    if config is None or not config.path.exists():
        run_outside_project()
    else:
        run(config)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from seamline import global_install

    try:
        return global_install.install()
    except SettingsError as e:
        print(f"seamline install: {e}", file=sys.stderr)
        return 1


def cmd_uninstall_global(args: argparse.Namespace) -> int:
    from seamline import global_install

    try:
        return global_install.uninstall()
    except SettingsError as e:
        print(f"seamline uninstall-global: {e}", file=sys.stderr)
        return 1


def cmd_hook(args: argparse.Namespace) -> int:
    from seamline.hooks.dispatch import main as hook_main

    return hook_main([args.event])


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
