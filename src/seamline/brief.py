"""What a session is told: a brief when it starts, and short updates when other sessions change
something relevant.

Both are capped in tokens (estimated at 4 characters each) and ordered by how much a mistake
would cost: open mismatches first, then dead ends, decisions, and what other services say
about the interfaces this service uses. Lines that don't fit are counted, never cut mid-way.
"""

from __future__ import annotations

import sqlite3

from seamline.config import INTEGRATION, Config
from seamline.ledger import queries as q
from seamline.ledger.queries import FactRow

CHARS_PER_TOKEN = 4
MORE_HINT = "run `seamline facts` or `seamline drift` for the rest, with quotes"


def build_brief(conn: sqlite3.Connection, config: Config, service: str | None) -> str:
    """The SessionStart brief for a session in `service` (None or integration = the root).
    Empty when the ledger has nothing to say."""
    here = None if service in (None, INTEGRATION) else service
    facts = [f for f in q.list_facts(conn) if f.origin == "session"]
    sections: list[tuple[str, list[str]]] = []

    mismatches = q.open_mismatches_with_services(conn)
    mismatches.sort(key=lambda m: here not in (m["provider"], m["assumer"]))
    sections.append(
        (
            "Open contract mismatches (a provider and a consumer disagree; check before relying "
            "on these fields):",
            [f"- {m['interface']}: {m['description']}" for m in mismatches],
        )
    )

    def near(f: FactRow) -> bool:
        return here is None or f.service in (here, None)

    for kind, title in (
        ("dead_end", "Dead ends (tried before and abandoned):"),
        ("decision", "Decisions:"),
    ):
        group = sorted((f for f in facts if f.kind == kind), key=lambda f: f.created_at)
        group.reverse()  # Newest first, then (stable) this service's before the rest
        group.sort(key=lambda f: not near(f))
        sections.append((title, [_line(f, here) for f in group]))

    if here:
        mine = q.interfaces_of(conn, here)
        others = [
            f
            for f in facts
            if f.service != here and f.interface_id in mine and f.kind in ("provides", "assumes")
        ]
        title = f"What other services say about interfaces {here} uses:"
    else:
        others = [f for f in facts if f.kind == "provides"]
        title = "Interfaces services provide:"
    sections.append((title, [_line(f, here) for f in others]))

    if not any(lines for _, lines in sections):
        return ""
    where = f"this session: {here}" if here else "this session: project root (all services)"
    header = (
        f"Seamline: shared memory from earlier Claude sessions in project {config.project!r} "
        f"({where}). Treat these as leads to verify, not facts."
    )
    return _fit(header, sections, config.brief.max_tokens)


def build_update(
    conn: sqlite3.Connection,
    config: Config,
    service: str | None,
    session_id: str,
    since: int,
) -> str:
    """Changes after change `since` that matter to this session, made by other sessions."""
    here = None if service in (None, INTEGRATION) else service
    mine = q.interfaces_of(conn, here) if here else set()
    lines: list[str] = []
    for c in q.changes_since(conn, since):
        if c["kind"] in ("mismatch_opened", "mismatch_resolved") and c["mismatch_id"]:
            m = q.mismatch_row(conn, c["mismatch_id"])
            if m is None or (here and here not in (m["provider"], m["assumer"])):
                continue
            verb = "New mismatch" if c["kind"] == "mismatch_opened" else "Resolved"
            lines.append(f"- {verb} on {m['interface']}: {m['description']}")
        elif c["kind"] == "fact_added" and c["fact_id"]:
            f = q.get_fact(conn, c["fact_id"])
            if f.origin != "session" or f.status != "active":
                continue
            if here and f.service != here and f.interface_id not in mine:
                continue
            if q.fact_sessions(conn, f.id) <= {session_id}:
                continue  # This session said it; no need to repeat it back
            lines.append(_line(f, here))
    if not lines:
        return ""
    header = "Seamline update: other Claude sessions in this project recorded:"
    return _fit(header, [("", lines)], config.brief.update_max_tokens)


def _line(f: FactRow, here: str | None) -> str:
    who = f.service or "integration"
    kind = f.kind.replace("_", " ")
    return f"- [{who} {kind}] {f.claim}" if who != here else f"- [{kind}] {f.claim}"


def _fit(header: str, sections: list[tuple[str, list[str]]], max_tokens: int) -> str:
    """Add lines in order until the budget is spent; then say how many were left out."""
    budget = max_tokens * CHARS_PER_TOKEN
    out = [header]
    used = len(header) + 1
    left_out = 0
    reserve = len(MORE_HINT) + 16
    for title, lines in sections:
        if not lines:
            continue
        added_title = False
        for line in lines:
            cost = len(line) + 1 + (0 if added_title or not title else len(title) + 2)
            if left_out or used + cost > budget - reserve:
                left_out += 1
                continue
            if not added_title and title:
                out.append("")
                out.append(title)
                added_title = True
            out.append(line)
            used += cost
    if left_out:
        out.append(f"(+{left_out} more; {MORE_HINT})")
    return "\n".join(out)
