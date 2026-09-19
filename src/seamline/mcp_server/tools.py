"""What the MCP tools answer, as plain functions over the ledger (the server only wires them
up). Every answer is compact Markdown under a size cap, and every fact line cites where it
came from: a session and line, or a file."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from seamline.config import INTEGRATION, Config
from seamline.ledger import queries as q
from seamline.ledger.queries import FactRow
from seamline.resolve.matcher import find_interface
from seamline.resolve.supersede import Candidate, store_fact

MAX_CHARS = 6000
_CONTRACT_KINDS = ("provides", "consumes", "assumes")
_KINDS = ("provides", "consumes", "assumes", "decision", "dead_end")


def integration_context(conn: sqlite3.Connection, config: Config, services: list[str]) -> str:
    """Interfaces between these services: each side's claims, then open mismatches first."""
    unknown = [s for s in services if s not in config.services and s != INTEGRATION]
    if unknown:
        return f"Unknown service(s): {', '.join(unknown)}. Known: {_known(config)}."
    wanted = set(services) or set(config.services)
    facts = [f for f in q.list_facts(conn) if f.kind in _CONTRACT_KINDS and f.interface_id]
    by_interface: dict[int, list[FactRow]] = {}
    for f in facts:
        by_interface.setdefault(f.interface_id, []).append(f)
    # An interface is "between" the services when at least two of them (or, for a single
    # service, that one) have something to say about it.
    need = min(2, len(wanted))
    shared = {
        i: fs
        for i, fs in by_interface.items()
        if len({f.service for f in fs if f.service in wanted}) >= need
    }
    lines: list[str] = []
    mismatches = [
        m
        for m in q.open_mismatches_with_services(conn)
        if m["provider"] in wanted or m["assumer"] in wanted
    ]
    if mismatches:
        lines.append("## Open mismatches")
        lines += [f"- **{m['interface']}**: {m['description']}" for m in mismatches]
        lines.append("")
    if not shared:
        lines.append(f"No shared interfaces recorded between {', '.join(sorted(wanted))} yet.")
    for interface_id, fs in sorted(shared.items(), key=lambda kv: q.interface_name(conn, kv[0])):
        lines.append(f"## {q.interface_name(conn, interface_id)}")
        for f in sorted(fs, key=lambda f: (_CONTRACT_KINDS.index(f.kind), f.service or "")):
            lines.append(_fact_line(conn, f))
        lines.append("")
    return _cap(lines)


def contract(conn: sqlite3.Connection, config: Config, interface: str) -> str:
    """Everything recorded about one interface, current facts first, with quotes."""
    interface_id = find_interface(conn, interface)
    if interface_id is None:
        names = [r[0] for r in conn.execute("SELECT name FROM interfaces ORDER BY name")]
        close = [n for n in names if _words(interface) & _words(n)]
        hint = f" Similar: {', '.join(close[:8])}." if close else ""
        return f"No interface named {interface!r} in the ledger.{hint}"
    name = q.interface_name(conn, interface_id)
    facts = q.list_facts(conn, interface_id=interface_id, include_inactive=True)
    current = [f for f in facts if f.status == "active"]
    old = [f for f in facts if f.status != "active"]
    lines = [f"# {name}"]
    mismatches = [
        m for m in q.open_mismatches_with_services(conn) if m["interface_id"] == interface_id
    ]
    if mismatches:
        lines.append("## Open mismatches")
        lines += [f"- {m['description']}" for m in mismatches]
    lines.append("## Current")
    lines += [_fact_line(conn, f, quote=True) for f in _ordered(current)] or ["- (none)"]
    if old:
        lines.append("## Superseded or stale (history)")
        lines += [_fact_line(conn, f, quote=True) for f in _ordered(old)]
    return _cap(lines)


def dead_ends(conn: sqlite3.Connection, topic: str) -> str:
    facts = _search(conn, topic, kinds=("dead_end",), include_inactive=False)
    if not facts:
        return f"No dead ends recorded{_about(topic)}."
    lines = [f"# Dead ends{_about(topic)}"]
    lines += [_fact_line(conn, f, quote=True) for f in facts]
    return _cap(lines)


def history(conn: sqlite3.Connection, query: str) -> str:
    facts = _search(conn, query, kinds=("decision",), include_inactive=True)
    if not facts:
        return f"No decisions recorded{_about(query)}."
    lines = [f"# Decisions{_about(query)}"]
    for f in facts:
        line = _fact_line(conn, f, quote=True)
        lines.append(line if f.status == "active" else line.replace("- ", f"- ({f.status}) ", 1))
    return _cap(lines)


def remember(
    conn: sqlite3.Connection,
    config: Config,
    fact: str,
    *,
    kind: str = "decision",
    service: str | None = None,
    interface: str | None = None,
    session_id: str | None = None,
) -> str:
    """Store a fact the user stated or confirmed. Origin 'user': it never replaces what a
    session or the code said, and the user's own words are the quote."""
    fact = fact.strip()
    if len(fact) < 8:
        return "Not stored: say the fact in a full sentence."
    if kind not in _KINDS:
        return f"Not stored: kind must be one of {', '.join(_KINDS)}."
    if service and service not in config.services:
        return f"Not stored: unknown service {service!r}. Known: {_known(config)}."
    from seamline.resolve.matcher import interface_id_for

    with conn:
        interface_id = interface_id_for(conn, interface, None) if interface else None
        stored = store_fact(
            conn,
            Candidate(
                kind=kind,
                origin="user",
                service=service,
                attributed_by="name" if service else "none",
                interface_id=interface_id,
                claim=fact,
                details=[],
                confidence=1.0,
                quote=fact,
                session_id=session_id or "mcp",
                timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    where = service or "the whole project"
    if stored.outcome == "duplicate":
        return f"Already known (fact #{stored.fact_id}); added this as more evidence."
    return f"Remembered as a {kind.replace('_', ' ')} for {where} (fact #{stored.fact_id})."


# --- formatting ---------------------------------------------------------------------------


def _fact_line(conn: sqlite3.Connection, f: FactRow, quote: bool = False) -> str:
    who = f.service or "integration"
    tag = f"[{who} {f.kind.replace('_', ' ')}]" + (" (from code)" if f.origin == "code" else "")
    fields = ", ".join(f"{d['name']}: {d['value']}" for d in f.details[:8])
    text = f"- {tag} {f.claim}" + (f" — {fields}" if fields and f.origin == "code" else "")
    ev = q.evidence_for(conn, f.id)
    if ev:
        e = ev[-1]
        if quote and f.origin != "code":
            text += f' — "{_short(e["quote"], 160)}"'
        text += f" ({_source(e)}{f', +{len(ev) - 1} more' if len(ev) > 1 else ''})"
    if f.stale_detail:
        text += f" [stale: {f.stale_detail}]"
    return text


def _source(e: sqlite3.Row) -> str:
    when = (e["timestamp"] or "")[:16].replace("T", " ")
    if e["file_path"]:
        return f"{e['file_path']}" + (f":{e['line_no']}" if e["line_no"] else "")
    sid = (e["session_id"] or "?")[:8]
    line = f" L{e['line_no']}" if e["line_no"] else ""
    return f"session {sid}{line}" + (f", {when}" if when else "")


def _ordered(facts: list[FactRow]) -> list[FactRow]:
    order = {k: i for i, k in enumerate(_KINDS)}
    return sorted(facts, key=lambda f: (order.get(f.kind, 9), f.service or "", f.id))


def _search(
    conn: sqlite3.Connection, text: str, *, kinds: tuple[str, ...], include_inactive: bool
) -> list[FactRow]:
    """Facts of these kinds matching any word of `text` (all of them when it's empty),
    best matches first."""
    facts = [
        f for k in kinds for f in q.list_facts(conn, kind=k, include_inactive=include_inactive)
    ]
    words = _words(text)
    if not words:
        return sorted(facts, key=lambda f: f.id, reverse=True)
    ids = _fts_ids(conn, words)
    scored = []
    for f in facts:
        hay = _words(f.claim + " " + " ".join(e["quote"] for e in q.evidence_for(conn, f.id)))
        score = len(words & hay) + (0.5 if f.id in ids else 0)
        if score:
            scored.append((score, f.id, f))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [f for _, _, f in scored]


def _fts_ids(conn: sqlite3.Connection, words: set[str]) -> set[int]:
    query = " OR ".join(f'"{w}"*' for w in sorted(words))
    try:
        return {
            r[0]
            for r in conn.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?", (query,))
        }
    except sqlite3.OperationalError:
        return set()


_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "it"}


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOP}


def _about(text: str) -> str:
    return f" about {text!r}" if text.strip() else ""


def _known(config: Config) -> str:
    return ", ".join(config.services) or "none (everything is integration scope)"


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _cap(lines: list[str]) -> str:
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > MAX_CHARS:
            rest = sum(1 for x in lines[i:] if x.startswith("- "))
            out.append(f"(+{rest} more lines cut; ask about a narrower topic or interface)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out).strip()
