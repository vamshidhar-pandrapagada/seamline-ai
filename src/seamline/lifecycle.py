"""`seamline pause`, `resume`, `remove`, `status` and `brief`: turning recording on and off
for one project, and checking on it."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from seamline import global_install, paths
from seamline.brief import build_brief
from seamline.config import INTEGRATION, Config
from seamline.extract.providers import SEAMLINE_KEY_ENV, key_source
from seamline.hooks import settings as hook_settings
from seamline.ledger import queries as q
from seamline.mcp_server import registration as mcp_registration
from seamline.worker_control import read_status, worker_running

KEY_HELP = (
    "The worker runs in the Claude app's environment. Give it a key only Seamline reads:\n"
    f"    launchctl setenv {SEAMLINE_KEY_ENV} <your key>\n"
    "  then quit and reopen the Claude app (repeat after a reboot)."
)

Out = Callable[[str], None]
Ask = Callable[[str], str]


def install_hooks(config: Config, out: Out = print, *, approve_mcp: bool = True) -> None:
    """Turn recording on for this project.

    Hooks: with `seamline install` done, the user-level hooks already cover every folder, so
    per-folder ones left from before are removed. Otherwise they go into the root and every
    service folder (stale ones from removed services go).

    MCP server: always registered in the project's own `.mcp.json`, so it only starts in
    this project, and (with `approve_mcp`) approved for it.
    """
    if global_install.active():
        _remove_folder_hooks(config, out)
        out("Hooks: the user-level install (`seamline install`) covers every folder here.")
    else:
        wanted = {f.resolve() for f in hook_settings.hook_folders(config)}
        for folder in _folders_with_hooks(config):
            if folder.resolve() not in wanted and hook_settings.uninstall(folder):
                out(f"Removed hooks from {_rel(config, folder)} (no longer a service)")
        done = []
        for folder in hook_settings.hook_folders(config):
            if not folder.is_dir():
                out(f"warning: {_rel(config, folder)} doesn't exist; no hooks there")
                continue
            changed = hook_settings.install(folder)
            done.append(folder)
            path = _rel(config, hook_settings.settings_path(folder))
            out(f"{'Added hooks to' if changed else 'Hooks already in'} {path}")
        _remember_folders(config, done)
    for note in mcp_registration.install(config):
        out(note)
    if approve_mcp and mcp_registration.approve(config):
        out("Approved the seamline MCP server for this project (.claude/settings.local.json)")
    elif not approve_mcp and not mcp_registration.approved(config):
        out(
            "The MCP server isn't approved yet: run `seamline resume` to approve it, or "
            "`claude` in this folder and approve it there."
        )
    note = hook_settings.ensure_gitignored(config)
    if note:
        out(note)
    paths.paused_marker(config.root).unlink(missing_ok=True)


def uninstall_hooks(config: Config, out: Out = print) -> int:
    """Remove everything that makes this project recorded: per-folder hooks and the MCP
    server's entry and approval (the user-level hooks stay; they skip paused projects)."""
    removed = _remove_folder_hooks(config, out)
    if mcp_registration.uninstall(config):
        out(f"Removed the seamline MCP server from {mcp_registration.FILENAME}")
        removed += 1
    if mcp_registration.revoke(config):
        removed += 1
    return removed


def _remove_folder_hooks(config: Config, out: Out) -> int:
    removed = 0
    for folder in {*hook_settings.hook_folders(config), *_folders_with_hooks(config)}:
        if hook_settings.uninstall(folder):
            out(f"Removed hooks from {_rel(config, hook_settings.settings_path(folder))}")
            removed += 1
    _record(config).unlink(missing_ok=True)
    return removed


def run_pause(config: Config, out: Out = print) -> int:
    uninstall_hooks(config, out)  # With the user-level install, the marker alone pauses
    marker = paths.paused_marker(config.root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    out(
        "Paused: new sessions in this project aren't recorded or briefed. Config and ledger "
        "are kept; `seamline resume` turns it back on. (Sessions already open keep their "
        "hooks until they restart; the hooks do nothing while paused.)"
    )
    return 0


def run_resume(config: Config, out: Out = print) -> int:
    install_hooks(config, out)
    out("Recording on. Sessions started from now get a brief; restart open ones to include them.")
    return 0


def run_remove(config: Config, ask: Ask = input, out: Out = print) -> int:
    uninstall_hooks(config, out)
    config.path.unlink(missing_ok=True)
    out(f"Removed {config.path.name}")
    data = paths.data_dir(config.root)
    if data.exists():
        answer = ask(f"Also delete {data} (the ledger and logs)? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            shutil.rmtree(data)
            out(f"Deleted {data}")
        else:
            out(f"Kept {data}")
    return 0


def run_brief(
    conn: sqlite3.Connection, config: Config, service: str | None, out: Out = print
) -> int:
    if service and service != INTEGRATION and service not in config.services:
        out(f"unknown service {service!r}; known: {', '.join(config.services) or 'none'}")
        return 1
    text = build_brief(conn, config, service)
    out(text or "(empty: the ledger has nothing to brief yet, so a session would get nothing)")
    return 0


def run_status(conn: sqlite3.Connection, config: Config, out: Out = print) -> int:
    root = config.root
    paused = paths.paused_marker(root).exists()
    out(f"Project {config.project!r} at {root}")
    out(f"Recording: {'PAUSED (seamline resume)' if paused else 'on'}")

    out("\nHooks:")
    if global_install.active():
        out("  user-level install (`seamline install`): every folder under this project")
        leftovers = [
            _rel(config, f)
            for f in hook_settings.hook_folders(config)
            if hook_settings.installed(f)
        ]
        if leftovers:
            out(
                "  leftover per-folder hooks (harmless; `seamline resume` removes them): "
                + ", ".join(leftovers)
            )
    for folder in [] if global_install.active() else hook_settings.hook_folders(config):
        events = hook_settings.installed(folder)
        state = (
            "installed"
            if len(events) == len(hook_settings.EVENTS)
            else (f"partial ({', '.join(events)})" if events else "missing (seamline resume)")
        )
        out(f"  {_rel(config, folder) + '/':<32} {state}")
    mcp_state = (
        "missing (seamline resume)"
        if not mcp_registration.installed(config)
        else "registered and approved"
        if mcp_registration.approved(config)
        else "registered, NOT approved (seamline resume)"
    )
    out(f"  {'MCP server (.mcp.json)':<32} {mcp_state}")
    starts = _mcp_starts(root)
    if starts:
        out(f"  MCP server last started {starts}")
    last = _last_hook_calls(root)
    if last:
        out("  Recent hook calls:")
        for line in last:
            out(f"    {line}")
    else:
        out("  No hook calls yet: open a new Claude session in the project.")

    status = read_status(root)
    running = worker_running(root)
    out("\nWorker: " + ("running" if running else "not running"))
    if status:
        out(
            f"  last state: {status.get('state')} at {status.get('updated')}"
            + (f": {status['message']}" if status.get("message") else "")
        )

    queue = q.work_queue(conn)
    if queue:
        out(f"  flagged sessions: {len(queue)}")
        for r in queue:
            why = "urgent" if r["urgent"] else "waits until idle"
            out(f"    {r['service'] or INTEGRATION:<16} {r['session_id'][:8]}  {why}")

    day = date.today().isoformat()
    spent = q.spent_on(conn, day)
    cap = config.worker.daily_budget_usd
    out(f"\nSpent today: ${spent:.2f} of the ${cap:.2f} daily cap")
    worker_key = status.get("key_source")
    if worker_key:
        out(f"Worker API key: from {worker_key}")
    elif status.get("state") == "error" and "credentials" in status.get("message", ""):
        out(f"Worker API key: MISSING. {KEY_HELP}")
    shell_key = key_source()
    out(f"API key in this shell: {shell_key or 'none'} (used by `seamline ingest` here)")
    errors = _recent_errors(root)
    if errors:
        out(f"\nRecent errors ({paths.logs_dir(root)}):")
        for e in errors:
            out(f"  {e}")
    return 0


def _folders_with_hooks(config: Config) -> list[Path]:
    """Folders Seamline installed hooks into before (including services since removed)."""
    try:
        lines = _record(config).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [(config.root / line).resolve() for line in lines if line.strip()]


def _remember_folders(config: Config, folders: list[Path]) -> None:
    record = _record(config)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("".join(f"{_rel(config, f)}\n" for f in folders), encoding="utf-8")


def _record(config: Config) -> Path:
    return paths.data_dir(config.root) / "hook_folders"


def _last_hook_calls(root: Path, limit: int = 5) -> list[str]:
    lines = _tail(paths.hooks_log_path(root))
    calls = [line for line in lines if line[:4].isdigit()]
    return [_ago(line) for line in calls[-limit:]]


def _mcp_starts(root: Path) -> str | None:
    starts = [line for line in _tail(paths.logs_dir(root) / "mcp.log") if " start: " in line]
    if not starts:
        return None
    stamp = starts[-1][:19]
    return f"{stamp} ({len(starts)} start(s) logged)"


def _recent_errors(root: Path, limit: int = 3) -> list[str]:
    hook_errors = [
        line
        for line in _tail(paths.hooks_log_path(root))
        if line[:4].isdigit() and " error" in line
    ]
    worker_errors = [
        line for line in _tail(paths.worker_log_path(root)) if " ERROR " in line or "Error" in line
    ]
    return (hook_errors + worker_errors)[-limit:]


def _tail(path: Path, max_bytes: int = 64 * 1024) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - max_bytes))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _ago(line: str) -> str:
    stamp, _, rest = line.partition(" ")
    try:
        seconds = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return line
    if seconds < 90:
        when = f"{seconds:.0f}s ago"
    elif seconds < 5400:
        when = f"{seconds / 60:.0f}m ago"
    else:
        when = f"{seconds / 3600:.0f}h ago"
    return f"{when:>8}  {rest}"


def _rel(config: Config, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(config.root)
    except ValueError:
        return str(path)
    return rel.as_posix() if rel.parts else "."
