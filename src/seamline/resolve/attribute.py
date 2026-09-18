"""Which service is a fact about? The session folder is only the default.

Order of evidence:
1. name   - the extractor set the fact's service to a listed service
2. files  - the fact's own text mentions files (`client.py`, `agent-core/src/grpc/…`)
            that the session touched, and they belong to one service (or a majority do)
3. name   - the fact's text names a service or its folder ("orchestrator's client")
4. files  - files touched near the statement belong to one service, or a clear majority
            do (Read/Edit/Write paths, and paths in Bash commands). "Near" means, in
            order: the tool calls since the previous thing Claude said (statements
            usually describe work just done), then those until the next thing it says
            (announcements: "Now making the edits…"), then the whole turn.
5. folder - the service of the folder the session started in
6. none   - no evidence: the integration scope (service = None)

Names come before files: "payments will need amount_cents", said while editing orders
files, is about payments.

Steps matter because real agentic sessions are often one prompt followed by a long run of
work across services: voting over that whole turn put every fact from a real
multi-service session (9 core files, 6 orchestrator files) on core. The fact's own text
matters because in that session every fact came from Claude's final summary, which covers
all services.
"""

from __future__ import annotations

import re
import shlex
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from seamline.config import INTEGRATION, Config
from seamline.mapping import service_for
from seamline.transcripts.models import Event, Kind

_PATHISH = re.compile(r"^[~.\w-]*/[\w./-]*$|^[\w.-]+\.[A-Za-z0-9]{1,6}$")


@dataclass(frozen=True)
class Attribution:
    service: str | None
    by: str  # name | files | folder | none
    files: tuple[str, ...] = field(default_factory=tuple)  # Touched files, relative to root


def attribute(
    fact_service: str | None,
    fact_line: int,
    events: list[Event],
    config: Config,
    default_service: str | None,
    text: str = "",
) -> Attribution:
    """`text` is what the fact says (claim, quote, interface, details)."""
    turn = turn_of(fact_line, events)
    before, after = steps_around(fact_line, turn)
    windows = [touched_files(w, config) for w in (before, after, turn)]
    nearest = next((w for w in windows if w), [])
    if fact_service in config.services:
        return Attribution(fact_service, "name", _rel(nearest, config))
    mentioned = mentioned_files(text, touched_files(events, config), config)
    winner = _majority(mentioned, config)
    if winner:
        return Attribution(winner, "files", _rel(mentioned, config))
    named = named_service(text, config)
    if named:
        return Attribution(named, "name", _rel(nearest, config))
    for files in windows:
        winner = _majority(files, config)
        if winner:
            return Attribution(winner, "files", _rel(files, config))
    rel = _rel(nearest, config)
    if default_service and default_service != INTEGRATION:
        return Attribution(default_service, "folder", rel)
    return Attribution(None, "none", rel)


def mentioned_files(text: str, session_files: list[Path], config: Config) -> list[Path]:
    """Files the text refers to: a path, or a bare file name the session touched."""
    if not text:
        return []
    by_name: dict[str, set[Path]] = {}
    for p in session_files:
        by_name.setdefault(p.name, set()).add(p)
    found: list[Path] = []
    for token in re.findall(r"[\w./-]*[\w-]\.[A-Za-z0-9]{1,6}\b|[\w.-]+/[\w./-]+", text):
        token = token.strip("./")
        candidate = (config.root / token).resolve()
        if "/" in token and candidate.is_relative_to(config.root) and candidate.exists():
            found.append(candidate)
        elif Path(token).name in by_name and len(by_name[Path(token).name]) == 1:
            found.append(next(iter(by_name[Path(token).name])))
    return found


def named_service(text: str, config: Config) -> str | None:
    """A service the text names, by its name or its folder's name, if exactly one wins."""
    counts: Counter[str] = Counter()
    for name, rel in config.services.items():
        folder = Path(rel).name
        for word in {name, folder}:
            counts[name] += len(re.findall(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, re.I))
    ranked = [(n, c) for n, c in counts.most_common() if c]
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def steps_around(line: int, turn: list[Event]) -> tuple[list[Event], list[Event]]:
    """The events since the previous thing Claude said, and until the next."""
    said = [i for i, e in enumerate(turn) if e.kind is Kind.ASSISTANT_TEXT and e.text.strip()]
    here = next((i for i, e in enumerate(turn) if e.line_no >= line), len(turn))
    prev = max((i for i in said if i < here), default=-1)
    nxt = min((i for i in said if i > here), default=len(turn))
    return turn[prev + 1 : here], turn[here + 1 : nxt]


def _majority(files: list[Path], config: Config) -> str | None:
    votes = Counter(s for s in (service_for(p, config) for p in files) if s and s != INTEGRATION)
    if not votes:
        return None
    (top, n), *rest = votes.most_common()
    return top if n > sum(c for _, c in rest) else None  # A strict majority


def _rel(files: list[Path], config: Config) -> tuple[str, ...]:
    return tuple(sorted({str(p.relative_to(config.root)) for p in files}))


def turn_of(line: int, events: list[Event]) -> list[Event]:
    """The events of the turn containing `line`: from the prompt before it to the next."""
    start = 0
    for i, e in enumerate(events):
        if e.kind is Kind.USER_PROMPT and not e.is_sidechain:
            if e.line_no <= line:
                start = i
            else:
                return events[start:i]
    return events[start:]


def touched_files(events: list[Event], config: Config) -> list[Path]:
    found: list[Path] = []
    for e in events:
        if e.kind is not Kind.TOOL_USE:
            continue
        cwd = Path(e.cwd) if e.cwd else config.root
        candidates = [e.file_path] if e.file_path else []
        if e.tool_name == "Bash" and isinstance(e.tool_input.get("command"), str):
            candidates += _paths_in_command(e.tool_input["command"])
        for raw in candidates:
            p = Path(raw).expanduser()
            p = (p if p.is_absolute() else cwd / p).resolve()
            if p.is_relative_to(config.root) and p != config.root:
                found.append(p)
    return found


def _paths_in_command(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:  # Unbalanced quotes, heredocs: fall back to whitespace
        tokens = command.split()
    return [t for t in tokens if "://" not in t and _PATHISH.match(t)]
