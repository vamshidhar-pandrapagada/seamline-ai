"""`seamline ingest`, `facts`, `scan` and `drift`: fill the ledger and read it back."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date

from seamline.config import Config
from seamline.contracts.scan import scan
from seamline.extract.budget import MeteredProvider
from seamline.extract.pricing import estimate_usd
from seamline.extract.providers import LLMProvider
from seamline.ledger import queries as q
from seamline.resolve.drift import recompute
from seamline.resolve.ingest import Pending, ingest, pending
from seamline.resolve.staleness import StalenessResult, refresh
from seamline.transcripts.discover import discover, find_session, inherited_uuids
from seamline.transcripts.sources.base import SessionSource
from seamline.transcripts.sources.claude_code import ClaudeCodeSource

Out = Callable[[str], None]
Ask = Callable[[str], str]
_KIND_ORDER = ["dead_end", "decision", "provides", "consumes", "assumes"]


def run_ingest(
    conn: sqlite3.Connection,
    config: Config,
    provider_factory: Callable[[], LLMProvider],
    *,
    session_id: str | None = None,
    redo: bool = False,
    model: str | None = None,
    yes: bool = False,
    max_chunks: int | None = None,
    ask: Ask = input,
    out: Out = print,
    source: SessionSource | None = None,
) -> int:
    source = source or ClaudeCodeSource()
    q.sync_services(conn, config)
    sessions = discover(config, source)
    if session_id:
        target = find_session([s.info for s in sessions], session_id)
        sessions = [s for s in sessions if s.info is target]
    infos = [s.info for s in discover(config, source)]
    redo_ids = frozenset(s.info.session_id for s in sessions) if redo else frozenset()
    todo = pending(
        conn, config, sessions, source, lambda i: inherited_uuids(i, infos, source), redo_ids
    )
    if not todo:
        out("Nothing to ingest: every session is up to date.")
        return 0

    calls = sum(min(p.excerpts, max_chunks or p.excerpts) for p in todo)
    model = model or config.extract.model
    chars = sum(
        p.excerpt_chars * min(p.excerpts, max_chunks or p.excerpts) // max(p.excerpts, 1)
        for p in todo
    )
    cost = estimate_usd(model, chars, calls)
    cost_text = f"about ${cost:.2f}" if cost is not None else "cost unknown for this model"
    out(f"{len(todo)} session(s) with new lines, {calls} model call(s), {cost_text} on {model}:")
    cap = config.worker.daily_budget_usd
    spent = q.spent_on(conn, date.today().isoformat())
    over = " (reached; it stops background work, not runs you confirm)" if spent >= cap else ""
    out(f"Spent today: ${spent:.2f} of the ${cap:.2f} daily cap{over}")
    for p in todo:
        out(
            f"  {p.session.service:<16} {p.session.info.session_id[:8]}  "
            f"{len(p.new.events):>5} new events  {p.excerpts:>3} excerpt(s)"
        )
    if calls and not yes and ask("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Cancelled; nothing sent.")
        return 1

    if redo:
        with conn:
            for sid in redo_ids:
                deleted, kept = q.forget_session(conn, sid)
                out(
                    f"Redo {sid[:8]}: removed {deleted} fact(s) from its last extraction"
                    + (f"; {kept} also backed by other sessions kept" if kept else "")
                )
    provider = MeteredProvider(provider_factory(), conn, model) if calls else None
    status = 0
    for p in todo:
        result = ingest(conn, config, p, provider, max_chunks=max_chunks)
        _print_ingest(p, result, out)
        if result.report.errors and not result.report.chunks_done:
            status = 1
        if result.report.stopped:
            left = len(todo) - todo.index(p) - 1
            if left:
                out(f"Stopped; {left} more session(s) left for the next run.")
            break
    if provider:
        out(f"This run cost ${provider.spent_usd:.2f}.")
    _print_open_mismatches(conn, out)
    return status


def run_scan(conn: sqlite3.Connection, config: Config, out: Out = print) -> int:
    if not config.contracts:
        out("No [contracts] paths in seamline.toml; nothing to scan.")
        return 0
    q.sync_services(conn, config)
    with conn:
        result = scan(conn, config)
        staleness = refresh(conn, config)
        drift = recompute(conn)
    out(
        f"Scanned {len(result.files)} file(s): {result.added} new, {result.changed} changed, "
        f"{result.unchanged} unchanged, {result.removed} removed code fact(s)."
    )
    for f in result.files:
        out(f"  {f}")
    _print_staleness(staleness, out)
    if drift.opened or drift.resolved:
        out(f"Drift: {len(drift.opened)} opened, {len(drift.resolved)} resolved.")
    return 0


def show_facts(
    conn: sqlite3.Connection,
    *,
    service: str | None = None,
    kind: str | None = None,
    include_inactive: bool = False,
    origin: str | None = None,
    out: Out = print,
) -> int:
    facts = q.list_facts(
        conn, service=service, kind=kind, include_inactive=include_inactive, origin=origin
    )
    hidden_code = 0
    if origin is None:  # By default, what sessions said; code facts only with --code
        hidden_code = sum(f.origin == "code" for f in facts)
        facts = [f for f in facts if f.origin != "code"]
    if not facts:
        out(
            "No facts yet. Run `seamline ingest` or `seamline scan`."
            + (f" ({hidden_code} fact(s) from code: --code)" if hidden_code else "")
        )
        return 0
    for k in _KIND_ORDER:
        group = [f for f in facts if f.kind == k]
        if not group:
            continue
        out(f"{k.replace('_', ' ').upper()} ({len(group)})")
        for f in group:
            tags = [f.service or "integration"]
            if f.interface:
                tags.append(f"`{f.interface}`")
            if f.status != "active":
                tags.append(f"{f.status}: {f.stale_detail}" if f.stale_detail else f.status)
            if f.origin == "code":
                tags.append("code")
            out(f"  #{f.id} {f.claim}  [{' · '.join(tags)}]")
            for ev in q.evidence_for(conn, f.id)[:2]:
                where = (
                    f"{ev['session_id'][:8]} L{ev['line_no']}"
                    if ev["session_id"]
                    else f"{ev['file_path']}:{ev['line_no']}"
                )
                out(f'      {where}: "{_short(ev["quote"], 100)}"')
        out("")
    out(
        f"{len(facts)} fact(s)"
        + ("" if include_inactive else " (active only; --all for history)")
        + (f"; {hidden_code} fact(s) from code hidden (--code)" if hidden_code else "")
    )
    return 0


def show_drift(conn: sqlite3.Connection, config: Config, out: Out = print) -> int:
    with conn:
        staleness = refresh(conn, config)
        recompute(conn)
    _print_staleness(staleness, out)
    rows = q.open_mismatches(conn)
    if not rows:
        out("No open mismatches.")
        return 0
    _print_open_mismatches(conn, out)
    return 1  # Non-zero, so scripts and CI can react to drift


def _print_open_mismatches(conn: sqlite3.Connection, out: Out) -> None:
    rows = q.open_mismatches(conn)
    if not rows:
        return
    out(f"\nOPEN MISMATCHES ({len(rows)})")
    for m in rows:
        out(f"  ✗ `{m['interface']}` {m['description']}")
        for label, fact_id in (("provides", m["provides_fact"]), ("assumes", m["assumes_fact"])):
            f = q.get_fact(conn, fact_id)
            ev = (q.evidence_for(conn, fact_id) or [None])[0]
            where = ""
            if ev is not None:
                where = (
                    f"{ev['session_id'][:8]} L{ev['line_no']}"
                    if ev["session_id"]
                    else f"{ev['file_path']}:{ev['line_no']}"
                )
                where = f'{where}: "{_short(ev["quote"], 90)}"'
            out(
                f"      {label:<8} {f.service or ('code' if f.origin == 'code' else 'integration')}"
                f"  {where}"
            )


def _print_ingest(p: Pending, result, out: Out) -> None:
    r = result.report
    outcomes = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(result.outcomes.items()))
    services = ", ".join(f"{k} {v}" for k, v in sorted(result.by_service.items()))
    out(
        f"\n{p.session.info.session_id[:8]}: {len(r.facts)} fact(s)"
        + (f" ({outcomes})" if outcomes else "")
        + (f" → {services}" if services else "")
        + f", {len(r.rejected)} rejected, {r.chunks_done}/{r.chunks} excerpt(s), "
        f"{r.input_tokens:,} in / {r.output_tokens:,} out tokens"
    )
    for err in r.errors:
        out(f"  error: {err}")
    if result.staleness:
        _print_staleness(result.staleness, out)
    if not result.advanced:
        out("  (read position not advanced: the next ingest retries this session)")


def _print_staleness(s: StalenessResult | None, out: Out) -> None:
    if not s:
        return
    for fact_id, why in s.marked_stale:
        out(f"  stale: #{fact_id} ({why})")
    for fact_id in s.restored:
        out(f"  restored: #{fact_id} (its fields are back in the code)")


def _short(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


__all__ = ["find_session", "run_ingest", "run_scan", "show_drift", "show_facts"]
