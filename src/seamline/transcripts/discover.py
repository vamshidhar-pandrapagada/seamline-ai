"""Find a project's sessions, map each to a service, and spot history copied between them."""

from __future__ import annotations

from dataclasses import dataclass

from seamline.config import Config
from seamline.mapping import service_for
from seamline.transcripts.sources.base import SessionInfo, SessionSource
from seamline.transcripts.sources.claude_code import ClaudeCodeSource


@dataclass(frozen=True)
class ProjectSession:
    info: SessionInfo
    service: str  # From the folder the session started in: the default for its facts


def discover(config: Config, source: SessionSource | None = None) -> list[ProjectSession]:
    """Every session started in the project root or below, oldest first."""
    source = source or ClaudeCodeSource()
    found = []
    for info in source.list_sessions(under=config.root):
        service = service_for(info.start_cwd, config) if info.start_cwd else None
        if service is not None:
            found.append(ProjectSession(info=info, service=service))
    return sorted(found, key=lambda s: (s.info.started or "", s.info.session_id))


def find_session(sessions: list[SessionInfo], prefix: str) -> SessionInfo:
    """Resolve a full or abbreviated session id."""
    matches = [s for s in sessions if s.session_id.startswith(prefix)]
    if not matches:
        raise LookupError(f"no session matches {prefix!r}")
    if len(matches) > 1:
        ids = ", ".join(s.session_id[:12] for s in matches[:5])
        raise LookupError(f"{prefix!r} matches several sessions: {ids}")
    return matches[0]


def inherited_uuids(
    session: SessionInfo, others: list[SessionInfo], source: SessionSource | None = None
) -> set[str]:
    """uuids in `session` that already appeared in a session created before it.

    Resuming or forking a session in Claude Code copies its history, same uuids and
    timestamps, into a new transcript. Only the session that created a record counts it.
    """
    if not session.started:
        return set()
    source = source or ClaudeCodeSource()
    earlier: set[str] = set()
    for other in others:
        if other.session_id == session.session_id or not other.started:
            continue
        if other.started < session.started:
            earlier |= _uuids(other, source)
    return _uuids(session, source) & earlier if earlier else set()


def _uuids(session: SessionInfo, source: SessionSource) -> set[str]:
    return {e.uuid for e in source.read(session).events if e.uuid}
