"""Catch up on demand (fix B), and say how fresh an answer is.

A tool call can wait where a hook can't. Before answering about some services, sessions of
those services with unread lines (other than the calling one) are marked urgent and the
background worker is started if it isn't running; the tool then waits for it, up to a
timeout. The worker does the extraction, so its lock, daily cap and logs apply as usual.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime

from seamline.config import INTEGRATION, Config
from seamline.worker_control import spawn_worker, worker_running

CATCH_UP_SECONDS = 30.0


def catch_up(
    conn: sqlite3.Connection,
    config: Config,
    services: set[str] | None,
    own_session: str | None,
    *,
    timeout: float = CATCH_UP_SECONDS,
    poll: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    start_worker: Callable = spawn_worker,
) -> list[sqlite3.Row]:
    """Ingest other sessions' unread lines for these services (None = all). Returns the
    sessions still unread when the time ran out."""
    rows = _unread(conn, services, own_session)
    if not rows:
        return []
    ids = [r["session_id"] for r in rows]
    with conn:
        conn.executemany("UPDATE sessions SET urgent = 1 WHERE session_id = ?", [(i,) for i in ids])
    if not worker_running(config.root):
        start_worker(config.root)
    deadline = clock() + timeout
    while clock() < deadline:
        sleep(poll)
        left = _unread(conn, services, own_session, only=set(ids))
        if not left:
            return []
    return _unread(conn, services, own_session, only=set(ids))


def describe(
    conn: sqlite3.Connection, services: set[str] | None, pending: list[sqlite3.Row]
) -> str:
    """One or two lines on how current the answer is."""
    rows = [
        r
        for r in conn.execute(
            "SELECT * FROM sessions WHERE last_ingested IS NOT NULL ORDER BY last_ingested DESC"
        )
        if services is None or (r["service"] or INTEGRATION) in services
    ][:4]
    parts = [
        f"{r['service'] or INTEGRATION} session {r['session_id'][:8]} "
        f"read {_ago(r['last_ingested'])}"
        for r in rows
    ]
    text = "Freshness: " + ("; ".join(parts) if parts else "no session has been read yet") + "."
    if pending:
        names = ", ".join(f"{r['service'] or INTEGRATION} {r['session_id'][:8]}" for r in pending)
        text += f" Still reading {names}: its last few minutes may be missing (ask again shortly)."
    return text


def _unread(
    conn: sqlite3.Connection,
    services: set[str] | None,
    own_session: str | None,
    only: set[str] | None = None,
) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM sessions WHERE dirty = 1 OR urgent = 1").fetchall()
    return [
        r
        for r in rows
        if r["session_id"] != own_session
        and (only is None or r["session_id"] in only)
        and (services is None or (r["service"] or INTEGRATION) in services | {INTEGRATION})
    ]


def _ago(stamp: str) -> str:
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "at an unknown time"
    seconds = (datetime.now(UTC) - then).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} days ago"
