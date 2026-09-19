"""Named queries shared by ingest, scan, drift and the CLI. All SQL lives here or in migrations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from seamline.config import Config


@dataclass
class FactRow:
    id: int
    kind: str
    origin: str
    service: str | None
    attributed_by: str
    interface_id: int | None
    interface: str | None
    interface_kind: str | None
    claim: str
    details: list[dict]
    status: str
    confidence: float
    source_ref: str | None
    created_at: str
    stale_detail: str | None = None


_FACT_SELECT = """
    SELECT f.*, i.name AS interface, i.kind AS interface_kind
    FROM facts f LEFT JOIN interfaces i ON i.id = f.interface_id
"""


def _fact(row: sqlite3.Row) -> FactRow:
    return FactRow(
        id=row["id"],
        kind=row["kind"],
        origin=row["origin"],
        service=row["service"],
        attributed_by=row["attributed_by"],
        interface_id=row["interface_id"],
        interface=row["interface"],
        interface_kind=row["interface_kind"],
        claim=row["claim"],
        details=json.loads(row["details"]),
        status=row["status"],
        confidence=row["confidence"],
        source_ref=row["source_ref"],
        created_at=row["created_at"],
        stale_detail=_optional(row, "stale_detail"),
    )


def _optional(row: sqlite3.Row, key: str):
    """A column added by a later migration (absent from rows of older queries)."""
    try:
        return row[key]
    except IndexError:
        return None


# --- services -----------------------------------------------------------------------------


def sync_services(conn: sqlite3.Connection, config: Config) -> None:
    with conn:
        conn.execute("DELETE FROM services")
        conn.executemany(
            "INSERT INTO services(name, path) VALUES (?, ?)", sorted(config.services.items())
        )


# --- facts --------------------------------------------------------------------------------


def get_fact(conn: sqlite3.Connection, fact_id: int) -> FactRow:
    return _fact(conn.execute(_FACT_SELECT + " WHERE f.id = ?", (fact_id,)).fetchone())


def list_facts(
    conn: sqlite3.Connection,
    *,
    service: str | None = None,
    kind: str | None = None,
    include_inactive: bool = False,
    interface_id: int | None = None,
    origin: str | None = None,
) -> list[FactRow]:
    where, args = [], []
    if not include_inactive:
        where.append("f.status = 'active'")
    if service is not None:
        where.append("f.service = ?")
        args.append(service)
    if kind:
        where.append("f.kind = ?")
        args.append(kind)
    if interface_id is not None:
        where.append("f.interface_id = ?")
        args.append(interface_id)
    if origin:
        where.append("f.origin = ?")
        args.append(origin)
    sql = _FACT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY f.id"
    return [_fact(r) for r in conn.execute(sql, args)]


def active_candidates(
    conn: sqlite3.Connection,
    kind: str,
    service: str | None,
    interface_id: int | None,
    origin: str,
) -> list[FactRow]:
    """Active facts that could be the same subject as a new fact. Origins never mix: a
    session statement doesn't replace what the code says, and vice versa."""
    sql = _FACT_SELECT + (
        " WHERE f.status = 'active' AND f.kind = ? AND f.service IS ? AND f.interface_id IS ?"
        " AND f.origin = ? ORDER BY f.id"
    )
    return [_fact(r) for r in conn.execute(sql, (kind, service, interface_id, origin))]


def insert_fact(
    conn: sqlite3.Connection,
    *,
    kind: str,
    origin: str,
    service: str | None,
    attributed_by: str,
    interface_id: int | None,
    claim: str,
    details: list[dict],
    confidence: float,
    status: str = "active",
    source_ref: str | None = None,
    prompt_version: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO facts(kind, origin, service, attributed_by, interface_id, claim, details,
                             status, confidence, source_ref, prompt_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kind,
            origin,
            service,
            attributed_by,
            interface_id,
            claim,
            json.dumps(details),
            status,
            confidence,
            source_ref,
            prompt_version,
        ),
    )
    return cur.lastrowid


def set_status(conn: sqlite3.Connection, fact_id: int, status: str) -> None:
    conn.execute("UPDATE facts SET status = ? WHERE id = ?", (status, fact_id))


def mark_stale(conn: sqlite3.Connection, fact_id: int, reason: str, detail: str = "") -> None:
    conn.execute(
        "UPDATE facts SET status = 'stale', stale_reason = ?, stale_detail = ? WHERE id = ?",
        (reason, detail, fact_id),
    )


def restore(conn: sqlite3.Connection, fact_id: int) -> None:
    conn.execute(
        "UPDATE facts SET status = 'active', stale_reason = NULL, stale_detail = NULL WHERE id = ?",
        (fact_id,),
    )


def facts_for_code_check(conn: sqlite3.Connection) -> list[FactRow]:
    """Session interface facts that are active, or stale because of an earlier code check."""
    sql = _FACT_SELECT + (
        " WHERE f.origin = 'session' AND f.kind IN ('provides', 'assumes')"
        " AND f.service IS NOT NULL"
        " AND (f.status = 'active' OR (f.status = 'stale' AND f.stale_reason = 'code'))"
        " ORDER BY f.id"
    )
    return [_fact(r) for r in conn.execute(sql)]


def add_evidence(
    conn: sqlite3.Connection,
    fact_id: int,
    *,
    quote: str,
    session_id: str | None = None,
    file_path: str | None = None,
    line_no: int | None = None,
    timestamp: str | None = None,
) -> bool:
    """Record where a fact came from. Returns False if this exact source was already recorded."""
    exists = conn.execute(
        """SELECT 1 FROM evidence WHERE fact_id = ? AND session_id IS ? AND file_path IS ?
           AND line_no IS ?""",
        (fact_id, session_id, file_path, line_no),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        """INSERT INTO evidence(fact_id, session_id, file_path, line_no, quote, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fact_id, session_id, file_path, line_no, quote, timestamp),
    )
    return True


def evidence_for(conn: sqlite3.Connection, fact_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM evidence WHERE fact_id = ? ORDER BY timestamp, id", (fact_id,)
    ).fetchall()


def latest_evidence_time(conn: sqlite3.Connection, fact_id: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(timestamp) FROM evidence WHERE fact_id = ?", (fact_id,)
    ).fetchone()
    return row[0]


def add_files(conn: sqlite3.Connection, fact_id: int, files: Iterable[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO fact_files(fact_id, path) VALUES (?, ?)",
        [(fact_id, f) for f in files],
    )


def link_supersedes(conn: sqlite3.Connection, newer: int, older: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO fact_links(from_fact, to_fact, relation) "
        "VALUES (?, ?, 'supersedes')",
        (newer, older),
    )


def superseded_by(conn: sqlite3.Connection, fact_id: int) -> int | None:
    row = conn.execute(
        "SELECT from_fact FROM fact_links WHERE to_fact = ? AND relation = 'supersedes'",
        (fact_id,),
    ).fetchone()
    return row[0] if row else None


def log_change(
    conn: sqlite3.Connection,
    kind: str,
    services: Iterable[str | None],
    *,
    fact_id: int | None = None,
    mismatch_id: int | None = None,
) -> None:
    names = sorted({s for s in services if s})
    conn.execute(
        "INSERT INTO changes(kind, fact_id, mismatch_id, services) VALUES (?, ?, ?, ?)",
        (kind, fact_id, mismatch_id, ",".join(names)),
    )


# --- interfaces ---------------------------------------------------------------------------


def interface_by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM interfaces WHERE key = ?", (key,)).fetchone()


def interface_by_alias(conn: sqlite3.Connection, alias: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT i.* FROM interface_aliases a JOIN interfaces i ON i.id = a.interface_id
           WHERE a.alias = ?""",
        (alias,),
    ).fetchone()


def create_interface(conn: sqlite3.Connection, key: str, name: str, kind: str | None) -> int:
    return conn.execute(
        "INSERT INTO interfaces(key, name, kind) VALUES (?, ?, ?)", (key, name, kind)
    ).lastrowid


def add_alias(conn: sqlite3.Connection, alias: str, interface_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO interface_aliases(alias, interface_id) VALUES (?, ?)",
        (alias, interface_id),
    )


def set_interface_kind_if_missing(conn: sqlite3.Connection, interface_id: int, kind: str) -> None:
    conn.execute(
        "UPDATE interfaces SET kind = ? WHERE id = ? AND kind IS NULL", (kind, interface_id)
    )


def interface_name(conn: sqlite3.Connection, interface_id: int) -> str:
    return conn.execute("SELECT name FROM interfaces WHERE id = ?", (interface_id,)).fetchone()[0]


# --- mismatches ---------------------------------------------------------------------------


def open_mismatches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.*, i.name AS interface FROM mismatches m
           JOIN interfaces i ON i.id = m.interface_id
           WHERE m.status = 'open' ORDER BY m.id"""
    ).fetchall()


# --- sessions -----------------------------------------------------------------------------


def session_state(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()


def all_session_states(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["session_id"]: r for r in conn.execute("SELECT * FROM sessions")}


def save_session_state(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    service: str | None,
    transcript_path: str,
    read_offset: int,
    read_line: int,
    last_activity: str | None,
) -> None:
    conn.execute(
        """INSERT INTO sessions(session_id, service, transcript_path, read_offset, read_line,
                                dirty, urgent, last_activity, last_ingested)
           VALUES (?, ?, ?, ?, ?, 0, 0, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
           ON CONFLICT(session_id) DO UPDATE SET
             service = excluded.service, transcript_path = excluded.transcript_path,
             read_offset = excluded.read_offset, read_line = excluded.read_line,
             dirty = 0, urgent = 0, last_activity = excluded.last_activity,
             last_ingested = excluded.last_ingested""",
        (session_id, service, transcript_path, read_offset, read_line, last_activity),
    )


# --- redo ---------------------------------------------------------------------------------


def forget_session(conn: sqlite3.Connection, session_id: str) -> tuple[int, int]:
    """Remove what a session contributed, so it can be extracted again.

    Facts backed only by this session are deleted (and anything they had superseded is
    restored); facts also backed elsewhere just lose this session's evidence. The session's
    read position is reset. Returns (facts deleted, facts that kept other evidence)."""
    fact_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT fact_id FROM evidence WHERE session_id = ?", (session_id,)
        )
    ]
    only_here = [
        f
        for f in fact_ids
        if not conn.execute(
            "SELECT 1 FROM evidence WHERE fact_id = ? AND session_id IS NOT ?", (f, session_id)
        ).fetchone()
    ]
    conn.execute("DELETE FROM evidence WHERE session_id = ?", (session_id,))
    for f in only_here:
        for (older,) in conn.execute(
            "SELECT to_fact FROM fact_links WHERE from_fact = ? AND relation = 'supersedes'", (f,)
        ).fetchall():
            still_replaced = conn.execute(
                "SELECT 1 FROM fact_links WHERE to_fact = ? AND from_fact != ? "
                "AND from_fact NOT IN (SELECT value FROM json_each(?))",
                (older, f, json.dumps(only_here)),
            ).fetchone()
            if not still_replaced:
                conn.execute(
                    "UPDATE facts SET status = 'active' WHERE id = ? AND status = 'superseded'",
                    (older,),
                )
        mismatch_ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM mismatches WHERE provides_fact = ? OR assumes_fact = ?", (f, f)
            )
        ]
        for m in mismatch_ids:
            conn.execute("UPDATE changes SET mismatch_id = NULL WHERE mismatch_id = ?", (m,))
            conn.execute("DELETE FROM mismatches WHERE id = ?", (m,))
        conn.execute("DELETE FROM fact_links WHERE from_fact = ? OR to_fact = ?", (f, f))
        conn.execute("DELETE FROM fact_files WHERE fact_id = ?", (f,))
        conn.execute("UPDATE changes SET fact_id = NULL WHERE fact_id = ?", (f,))
        conn.execute("DELETE FROM facts WHERE id = ?", (f,))
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    return len(only_here), len(fact_ids) - len(only_here)


def record_spend(
    conn: sqlite3.Connection,
    day: str,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    usd: float | None,
) -> None:
    conn.execute(
        "INSERT INTO spend (day, model, purpose, input_tokens, output_tokens, usd)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (day, model, purpose, input_tokens, output_tokens, usd),
    )


def spent_on(conn: sqlite3.Connection, day: str) -> float:
    """USD spent on model calls that day (calls with an unknown price count as 0)."""
    row = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE day = ?", (day,)).fetchone()
    return float(row[0])
