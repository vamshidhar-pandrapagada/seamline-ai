"""Store a fact: add evidence to a duplicate, replace what it supersedes, or insert it new.

Only facts with the same kind, service, interface and origin are compared. For each such
pair, rules give one of three answers:

- same: the newer fact replaces the older one
    interface facts: `consumes` (no shape claim), the older one had no details, or the
                     newer one restates every field of the older one
- different: both stay
    decisions / dead ends: claims share almost no words
- ambiguous: everything in between (a partial overlap, a rename with no shared word like
  `amount` -> `total_cents`, `created_at` vs `updated_at`, similar-but-different
  decisions). A judge (a small LLM call, resolve/judge.py) decides; without one, or when
  it fails, a word-overlap rule decides, leaning towards keeping both.

Renames matter: when a consumer fixes `amount` to `amount_cents`, the old assumption must
go, or its mismatch never resolves. A newer statement that is only *less* detailed (no
field details) never silently wipes out a more detailed one.

If the incoming fact is actually older (out-of-order backfill), it's stored already
superseded by the current one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from seamline.ledger import queries as q
from seamline.resolve.judge import Judge, describe
from seamline.resolve.matcher import detail_fields, fields_related, same_details, similarity

DUPLICATE_SIMILARITY = 0.8
SAME_SUBJECT_SIMILARITY = 0.5  # Fallback rule for ambiguous decisions
UNRELATED_SIMILARITY = 0.25  # Below this, two decisions are about different things
INTERFACE_KINDS = ("provides", "consumes", "assumes")


@dataclass
class Candidate:
    kind: str
    origin: str
    service: str | None
    attributed_by: str
    interface_id: int | None
    claim: str
    details: list[dict]
    confidence: float
    quote: str
    session_id: str | None = None
    file_path: str | None = None
    line_no: int | None = None
    timestamp: str | None = None
    files: tuple[str, ...] = field(default_factory=tuple)
    source_ref: str | None = None
    prompt_version: str | None = None


@dataclass
class StoreResult:
    fact_id: int
    outcome: str  # added | duplicate | superseded_existing | stored_as_older
    replaced: list[int] = field(default_factory=list)
    judged: int = 0  # Ambiguous pairs sent to the judge


def store_fact(conn: sqlite3.Connection, c: Candidate, judge: Judge | None = None) -> StoreResult:
    existing = q.active_candidates(conn, c.kind, c.service, c.interface_id, c.origin)

    for old in existing:
        if _is_duplicate(c, old):
            q.add_evidence(conn, old.id, **_evidence(c))
            q.add_files(conn, old.id, c.files)
            return StoreResult(old.id, "duplicate")

    same_subject, judged = [], 0
    for old in existing:
        answer = _relation(c, old)
        if answer == "ambiguous":
            verdict = (
                judge.replaces(_view(c, old.interface), _view(old, old.interface))
                if judge
                else None
            )
            judged += verdict is not None
            same = verdict.replaces if verdict else _fallback(c, old)
        else:
            same = answer == "same"
        if same:
            same_subject.append(old)
    newer_than_all = all(
        (q.latest_evidence_time(conn, old.id) or "") <= (c.timestamp or "") for old in same_subject
    )
    status = "active" if newer_than_all else "superseded"
    fact_id = q.insert_fact(
        conn,
        kind=c.kind,
        origin=c.origin,
        service=c.service,
        attributed_by=c.attributed_by,
        interface_id=c.interface_id,
        claim=c.claim,
        details=c.details,
        confidence=c.confidence,
        status=status,
        source_ref=c.source_ref,
        prompt_version=c.prompt_version,
    )
    q.add_evidence(conn, fact_id, **_evidence(c))
    q.add_files(conn, fact_id, c.files)

    if not newer_than_all:
        current = same_subject[-1]
        q.link_supersedes(conn, current.id, fact_id)
        return StoreResult(fact_id, "stored_as_older", judged=judged)

    q.log_change(conn, "fact_added", [c.service], fact_id=fact_id)
    for old in same_subject:
        q.set_status(conn, old.id, "superseded")
        q.link_supersedes(conn, fact_id, old.id)
        q.log_change(conn, "fact_superseded", [old.service], fact_id=old.id)
    outcome = "superseded_existing" if same_subject else "added"
    return StoreResult(fact_id, outcome, [o.id for o in same_subject], judged)


def _is_duplicate(c: Candidate, old: q.FactRow) -> bool:
    if c.kind in INTERFACE_KINDS and (c.details or old.details):
        return same_details(c.details, old.details)
    return similarity(c.claim, old.claim) >= DUPLICATE_SIMILARITY


def _relation(c: Candidate, old: q.FactRow) -> str:
    """same | different | ambiguous, by rules alone."""
    if c.kind in INTERFACE_KINDS:
        if c.kind == "consumes" or not old.details:
            return "same"
        new_fields, old_fields = detail_fields(c.details), detail_fields(old.details)
        if new_fields and new_fields >= old_fields:
            return "same"  # A full restatement
        return "ambiguous"
    if similarity(c.claim, old.claim) < UNRELATED_SIMILARITY:
        return "different"
    return "ambiguous"


def _fallback(c: Candidate, old: q.FactRow) -> bool:
    """Rules for ambiguous pairs when there's no judge. Leans towards keeping both."""
    if c.kind in INTERFACE_KINDS:
        if not c.details:
            return False  # Less detailed: don't wipe out the older, detailed fact
        return fields_related(detail_fields(c.details), detail_fields(old.details))
    return similarity(c.claim, old.claim) >= SAME_SUBJECT_SIMILARITY


def _view(f: Candidate | q.FactRow, interface: str | None) -> dict:
    """Both facts share the interface (that's why they're compared); name it for the judge."""
    return describe(f.kind, f.service, interface, f.claim, f.details)


def _evidence(c: Candidate) -> dict:
    return {
        "quote": c.quote,
        "session_id": c.session_id,
        "file_path": c.file_path,
        "line_no": c.line_no,
        "timestamp": c.timestamp,
    }
