"""The hook entry point: `python -m seamline.hooks <Event>`, with Claude Code's JSON on stdin.

Rules, because this runs inside every session of the project:
- always exit 0 (exit code 2 would block the user's prompt), whatever happens;
- print nothing except context meant for Claude (the brief or an update);
- finish fast: only flags and ledger reads here, never a model call. Extraction happens in
  the background worker, which this starts when there's work and none is running.
Every call is logged to `.seamline/logs/hooks.log` (event, folder, time taken, errors).

The hooks come from the user settings (`seamline install`, with `--global`, one set for
every folder) or from a folder's own settings (the older per-project setup). When a folder
has both, the user-level call steps aside so each event is handled once.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from seamline import paths
from seamline.config import INTEGRATION, Config, find_project
from seamline.worker_control import INTERNAL_ENV, spawn_worker, worker_running

LOG_MAX_BYTES = 512 * 1024


def main(argv: list[str] | None = None, stdin=None, stdout=None) -> int:
    started = time.perf_counter()
    argv = sys.argv[1:] if argv is None else argv
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    event = argv[0] if argv else ""
    from_user_settings = "--global" in argv[1:]
    config: Config | None = None
    folder = ""
    try:
        if os.environ.get(INTERNAL_ENV):
            return 0
        payload = json.loads(stdin.read() or "{}")
        folder = payload.get("cwd") or os.getcwd()
        config = find_project(folder)
        if config is None or paths.paused_marker(config.root).exists():
            return 0
        if from_user_settings and _has_folder_hooks(folder):
            return 0  # The folder's own (older, per-project) hooks handle this event
        text = handle(event, payload, config, folder)
        if text:
            stdout.write(text + "\n")
            stdout.flush()
        _log(config, event, folder, started, "ok" + (f" +{len(text)} chars" if text else ""))
    except Exception:  # noqa: BLE001 (a hook must never fail the session)
        if config is not None:
            _log(config, event, folder, started, "error\n" + traceback.format_exc())
    return 0


def handle(event: str, payload: dict, config: Config, folder: str) -> str:
    """Update the ledger's session flags for this event; return text for Claude, if any."""
    # Imported here so a call outside any project costs as little as possible.
    from seamline.brief import build_brief, build_update
    from seamline.ledger import queries as q
    from seamline.ledger.db import open_ledger
    from seamline.mapping import service_for

    session_id = payload.get("session_id")
    if not session_id:
        return ""
    service = service_for(Path(folder), config) or INTEGRATION
    transcript = payload.get("transcript_path") or ""
    text = ""
    conn = open_ledger(config.root)
    try:
        with conn:
            if event in ("SessionStart", "UserPromptSubmit"):
                q.touch_session(conn, session_id, service=service, transcript_path=transcript)
                # Catch up on switch: other sessions' unread lines are ingested right away,
                # so this session hears about them on its next prompt.
                q.mark_others_urgent(conn, session_id)
                since = q.seen_position(conn, session_id)
                latest = q.last_change_id(conn)
                if event == "SessionStart" or since is None:
                    text = build_brief(conn, config, service)
                elif latest > since:
                    text = build_update(conn, config, service, session_id, since)
                q.set_seen(conn, session_id, latest)
            elif event == "Stop":
                q.touch_session(
                    conn, session_id, service=service, transcript_path=transcript, dirty=True
                )
            elif event in ("PreCompact", "SessionEnd"):
                q.touch_session(
                    conn,
                    session_id,
                    service=service,
                    transcript_path=transcript,
                    dirty=True,
                    urgent=True,
                )
            work = bool(q.work_queue(conn))
    finally:
        conn.close()
    if work and not worker_running(config.root):
        spawn_worker(config.root)
    return text


def _has_folder_hooks(folder: str) -> bool:
    from seamline.hooks.settings import installed

    return bool(installed(Path(folder)))


def _log(config: Config, event: str, folder: str, started: float, outcome: str) -> None:
    try:
        path = paths.hooks_log_path(config.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
        where = os.path.relpath(folder, config.root) if folder else "?"
        ms = (time.perf_counter() - started) * 1000
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {event or '?'} {where} {ms:.0f}ms {outcome}\n")
    except OSError:
        pass
