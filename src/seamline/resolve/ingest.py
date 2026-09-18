"""Ingest a session: read new lines → extract → attribute → store → check facts against the
code → recompute drift.

Reading resumes from the byte offset saved for the session, so each line is sent to the
model once. The offset only advances when every excerpt succeeded; otherwise the next run
retries, and repeated facts become evidence on the existing fact rather than duplicates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from seamline.config import Config
from seamline.extract.chunker import build_chunks
from seamline.extract.extractor import PROMPT_VERSION, ExtractionReport, extract_events
from seamline.extract.providers import LLMProvider
from seamline.ledger import queries as q
from seamline.resolve.attribute import attribute
from seamline.resolve.drift import DriftResult, recompute
from seamline.resolve.judge import LLMJudge
from seamline.resolve.matcher import interface_id_for
from seamline.resolve.staleness import StalenessResult, refresh
from seamline.resolve.supersede import Candidate, store_fact
from seamline.transcripts.classify import classify
from seamline.transcripts.discover import ProjectSession
from seamline.transcripts.reader import ReadResult
from seamline.transcripts.sources.base import SessionSource


@dataclass
class Pending:
    session: ProjectSession
    new: ReadResult  # Events after the saved offset
    inherited: set[str]
    excerpts: int  # How many model calls ingesting it will take
    excerpt_chars: int = 0  # Their total size, for the cost estimate


@dataclass
class IngestResult:
    session_id: str
    report: ExtractionReport
    outcomes: dict[str, int] = field(default_factory=dict)  # added/duplicate/superseded…
    by_service: dict[str, int] = field(default_factory=dict)
    judged: int = 0  # Ambiguous fact pairs the judge decided
    advanced: bool = False
    staleness: StalenessResult | None = None
    drift: DriftResult | None = None


def pending(
    conn: sqlite3.Connection,
    config: Config,
    sessions: list[ProjectSession],
    source: SessionSource,
    inherited_for,  # (SessionInfo) -> set[str]
    from_start: frozenset[str] = frozenset(),  # Session ids to read from the beginning
) -> list[Pending]:
    """Sessions with lines not yet ingested, and what ingesting each would cost."""
    states = q.all_session_states(conn)
    out = []
    for s in sessions:
        state = None if s.info.session_id in from_start else states.get(s.info.session_id)
        offset = state["read_offset"] if state else 0
        if s.info.size <= offset:
            continue
        new = source.read(s.info, offset, state["read_line"] if state else 0)
        if not new.events and new.offset == offset:
            continue
        inherited = inherited_for(s.info)
        labeled = [(e, classify(e, inherited)) for e in new.events]
        chunks = build_chunks(labeled, root=config.root)
        out.append(Pending(s, new, inherited, len(chunks), sum(len(c.text) for c in chunks)))
    return out


def ingest(
    conn: sqlite3.Connection,
    config: Config,
    item: Pending,
    provider: LLMProvider,
    *,
    max_chunks: int | None = None,
) -> IngestResult:
    info = item.session.info
    report = extract_events(
        item.new.events,
        config,
        provider,
        session_id=info.session_id,
        inherited=item.inherited,
        max_chunks=max_chunks,
    )
    result = IngestResult(info.session_id, report)
    judge = LLMJudge(provider)
    with conn:
        for fact in report.facts:
            f = fact.fact
            text = " ".join(
                [f.claim, f.quote, f.interface or "", *(f"{d.name} {d.value}" for d in f.details)]
            )
            who = attribute(
                f.service, fact.line, item.new.events, config, item.session.service, text
            )
            interface_id = (
                interface_id_for(conn, f.interface, f.interface_kind) if f.interface else None
            )
            stored = store_fact(
                conn,
                Candidate(
                    kind=f.kind,
                    origin="session",
                    service=who.service,
                    attributed_by=who.by,
                    interface_id=interface_id,
                    claim=f.claim,
                    details=[d.model_dump() for d in f.details],
                    confidence=f.confidence,
                    quote=f.quote,
                    session_id=info.session_id,
                    line_no=fact.line,
                    timestamp=fact.timestamp,
                    files=who.files,
                    prompt_version=PROMPT_VERSION,
                ),
                judge,
            )
            result.judged += stored.judged
            result.outcomes[stored.outcome] = result.outcomes.get(stored.outcome, 0) + 1
            label = who.service or "integration"
            result.by_service[label] = result.by_service.get(label, 0) + 1
        # Advance only when the whole session was processed without errors
        complete = not report.errors and (max_chunks is None or report.chunks_done >= report.chunks)
        if complete:
            q.save_session_state(
                conn,
                info.session_id,
                service=item.session.service,
                transcript_path=str(info.path),
                read_offset=item.new.offset,
                read_line=item.new.line_no,
                last_activity=info.last_activity.isoformat(),
            )
            result.advanced = True
        result.staleness = refresh(conn, config)
        result.drift = recompute(conn)
    return result
