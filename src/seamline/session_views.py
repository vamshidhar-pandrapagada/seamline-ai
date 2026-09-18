"""`seamline sessions` and `seamline show`: look at what Seamline will read."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from seamline import paths
from seamline.config import Config, ConfigError, find_project
from seamline.extract.extractor import ExtractionReport, extract_events
from seamline.extract.providers import LLMProvider
from seamline.ledger import queries as q
from seamline.ledger.db import open_ledger
from seamline.transcripts.classify import Label, classify
from seamline.transcripts.discover import (
    ProjectSession,
    discover,
    find_session,
    inherited_uuids,
)
from seamline.transcripts.models import Event
from seamline.transcripts.sources.base import SessionSource
from seamline.transcripts.sources.claude_code import ClaudeCodeSource

Out = Callable[[str], None]
TEXT_WIDTH = 90


def resolve_project(root: str | None) -> Config:
    """The project containing `root` (or the current folder).

    With an explicit --root that has no seamline.toml, treat it as a project with no
    services, so any folder's sessions can be inspected before running `init`.
    """
    start = Path(root or ".").resolve()
    config = find_project(start)
    if config:
        return config
    if root:
        return Config(project=start.name, root=start)
    raise ConfigError(
        f"{start} is not inside a Seamline project (run `seamline init`, or pass --root)"
    )


def show_sessions(config: Config, out: Out = print, source: SessionSource | None = None) -> int:
    sessions = discover(config, source)
    out(f"{config.project}: {len(sessions)} session(s) under {config.root}\n")
    if not sessions:
        out("No Claude Code sessions found for this project yet.")
        return 0
    header = f"{'SERVICE':<16} {'SESSION':<10} {'STARTED IN':<24} {'STARTED':<17} "
    header += f"{'LAST ACTIVE':<17} {'SIZE':>7}  {'AGENTS':>6}  INGESTED"
    out(header)
    read = _read_offsets(config)
    for s in sorted(sessions, key=lambda s: (s.service, s.info.started or "")):
        out(_session_row(s, config, read.get(s.info.session_id) if read is not None else None))
    if read is None:
        out("\nINGESTED shows once this project has a ledger (`seamline init`, then `ingest`).")
    return 0


def _read_offsets(config: Config) -> dict[str, int] | None:
    """Bytes ingested per session, from the ledger, without creating one."""
    db = paths.ledger_path(config.root)
    if not config.path.exists() or not db.exists():
        return None
    conn = open_ledger(config.root)
    try:
        return {sid: row["read_offset"] for sid, row in q.all_session_states(conn).items()}
    finally:
        conn.close()


def show_session(
    config: Config | None,
    session_id: str,
    *,
    show_all: bool = False,
    full: bool = False,
    out: Out = print,
    source: SessionSource | None = None,
) -> int:
    source = source or ClaudeCodeSource()
    if config:
        project_sessions = discover(config, source)
        candidates = [s.info for s in project_sessions]
    else:
        project_sessions = []
        candidates = source.list_sessions()
    info = find_session(candidates, session_id)
    # Resumed sessions live next to their parent, so compare with siblings outside a project.
    others = candidates if config else [s for s in candidates if s.path.parent == info.path.parent]
    inherited = inherited_uuids(info, others, source)
    service = next((s.service for s in project_sessions if s.info is info), "-")

    result = source.read(info)
    labeled = [(e, classify(e, inherited)) for e in result.events]
    counts = Counter(label for _, label in labeled)
    n_inherited = sum(1 for e, _ in labeled if e.uuid in inherited)

    out(f"session  {info.session_id}")
    out(f"service  {service}    started in {info.start_cwd}    at {_ts(info.started)}")
    out(
        f"events   {len(labeled)}: "
        + ", ".join(f"{counts[lbl]} {lbl}" for lbl in Label)
        + (f"    ({n_inherited} inherited from an earlier session)" if n_inherited else "")
    )
    if result.bad_lines:
        out(f"warning  {len(result.bad_lines)} unreadable line(s): {result.bad_lines[:10]}")
    out("")
    for event, label in labeled:
        if label is Label.SKIP and not show_all:
            continue
        out(_event_row(event, label, event.uuid in inherited, full))
    if not show_all:
        out(f"\n({counts[Label.SKIP]} skip events hidden; use --all to show them)")
    return 0


def _session_row(s: ProjectSession, config: Config, offset: int | None) -> str:
    info = s.info
    started_in = _relative(info.start_cwd, config.root)
    if offset is None:
        ingested = "-"
    else:
        ingested = "all" if offset >= info.size else f"{offset * 100 // max(info.size, 1)}%"
    return (
        f"{s.service:<16} {info.session_id[:8]:<10} {started_in:<24.24} {_ts(info.started):<17} "
        f"{info.last_activity.astimezone().strftime('%Y-%m-%d %H:%M'):<17} "
        f"{_size(info.size):>7}  {info.subagents:>6}  {ingested}"
    )


def _event_row(event: Event, label: Label, inherited: bool, full: bool) -> str:
    text = event.text if full else " ".join(event.text.split())
    if not full and len(text) > TEXT_WIDTH:
        text = text[: TEXT_WIDTH - 1] + "…"
    kind = event.kind.value
    if event.tool_name:
        kind = f"{kind}:{event.tool_name}"
    marker = "^" if inherited else " "
    loc = f"{event.line_no}.{event.block}" if event.block else str(event.line_no)
    return f"{loc:>7}{marker}{label.value:<9} {kind:<24.24} {text}"


def _relative(path: Path | None, root: Path) -> str:
    if path is None:
        return "?"
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return str(path)
    return "." if str(rel) == "." else f"{rel}/"


def _ts(iso: str | None) -> str:
    """ISO timestamp (UTC in transcripts) as local time."""
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def _size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def run_extract(
    config: Config,
    session_id: str,
    provider: LLMProvider,
    *,
    max_chunks: int | None = None,
    json_path: str | None = None,
    out: Out = print,
    source: SessionSource | None = None,
) -> ExtractionReport:
    """Extract facts from one session and print them. Nothing is stored (phase 3)."""
    source = source or ClaudeCodeSource()
    sessions = [s.info for s in discover(config, source)]
    info = find_session(sessions, session_id)
    inherited = inherited_uuids(info, sessions, source)
    events = source.read(info).events
    out(
        f"Extracting {info.session_id[:8]} with {provider.name}"
        + (f" (first {max_chunks} excerpt(s))" if max_chunks else "")
        + " …"
    )
    report = extract_events(
        events,
        config,
        provider,
        session_id=info.session_id,
        inherited=inherited,
        max_chunks=max_chunks,
    )
    _print_report(report, out)
    if json_path:
        Path(json_path).write_text(json.dumps(report_to_dict(report), indent=2))
        out(f"\nWrote {json_path}")
    return report


def report_to_dict(report: ExtractionReport) -> dict:
    return {
        "session_id": report.session_id,
        "prompt_version": report.prompt_version,
        "chunks": report.chunks,
        "chunks_done": report.chunks_done,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "errors": report.errors,
        "facts": [
            {**f.fact.model_dump(), "line": f.line, "timestamp": f.timestamp, "cwd": f.cwd}
            for f in report.facts
        ],
        "rejected": [{**r.fact.model_dump(), "reason": r.reason} for r in report.rejected],
    }


def _print_report(report: ExtractionReport, out: Out) -> None:
    out("")
    order = ["dead_end", "decision", "provides", "consumes", "assumes"]
    for kind in order:
        group = [f for f in report.facts if f.fact.kind == kind]
        if not group:
            continue
        out(f"{kind.replace('_', ' ').upper()} ({len(group)})")
        for f in group:
            fact = f.fact
            where = " · ".join(
                x for x in (fact.service, fact.interface and f"`{fact.interface}`") if x
            )
            out(
                f"  • {fact.claim}"
                + (f"  [{where}]" if where else "")
                + f"  ({fact.confidence:.1f})"
            )
            if fact.details:
                out("      " + ", ".join(f"{d.name}={d.value}" for d in fact.details))
            out(f'      L{f.line}: "{_short(fact.quote, 110)}"')
        out("")
    if not report.facts:
        out("No facts extracted.\n")
    if report.rejected:
        out(f"REJECTED ({len(report.rejected)}): quote check failed")
        for r in report.rejected:
            out(f'  × {r.fact.kind}: "{_short(r.fact.quote, 80)}" ({r.reason})')
        out("")
    for err in report.errors:
        out(f"error: {err}")
    out(
        f"{len(report.facts)} fact(s), {len(report.rejected)} rejected · "
        f"{report.chunks_done}/{report.chunks} excerpt(s) · "
        f"{report.input_tokens:,} input + {report.output_tokens:,} output tokens · "
        f"prompt {report.prompt_version} · nothing stored (phase 3)"
    )


def _short(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"
