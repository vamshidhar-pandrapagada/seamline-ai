"""Claude Code sessions: ~/.claude/projects/<encoded folder>/<session id>.jsonl."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from seamline import paths
from seamline.transcripts.reader import ReadResult, read_events
from seamline.transcripts.sources.base import SessionInfo

_HEAD_LINES = 50  # The first cwd and timestamp appear within a few lines


class ClaudeCodeSource:
    name = "claude_code"

    def __init__(self, home: Path | None = None):
        self.projects_dir = (home or paths.claude_home()) / "projects"

    def list_sessions(self, under: Path | None = None) -> list[SessionInfo]:
        if not self.projects_dir.is_dir():
            return []
        under_resolved = under.resolve() if under else None
        prefixes = _encoded_prefixes(under) if under else None
        sessions = []
        for folder in sorted(self.projects_dir.iterdir()):
            if not folder.is_dir():
                continue
            # Cheap pre-filter: a subfolder's encoded name always starts with its parent's.
            # Encoding is lossy (shop_ai and shop-ai collide), so the recorded cwd decides.
            if prefixes and not any(folder.name.startswith(p) for p in prefixes):
                continue
            for transcript in sorted(folder.glob("*.jsonl")):
                info = self._info(transcript)
                if under_resolved is None or _is_inside(info.start_cwd, under_resolved):
                    sessions.append(info)
        return sessions

    def read(self, session: SessionInfo, offset: int = 0, line_no: int = 0) -> ReadResult:
        return read_events(session.path, offset, line_no)

    def _info(self, transcript: Path) -> SessionInfo:
        stat = transcript.stat()
        subagent_dir = transcript.with_suffix("") / "subagents"
        start_cwd, started = read_head(transcript)
        return SessionInfo(
            session_id=transcript.stem,
            path=transcript,
            start_cwd=start_cwd,
            started=started,
            size=stat.st_size,
            last_activity=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            subagents=len(list(subagent_dir.glob("*.jsonl"))) if subagent_dir.is_dir() else 0,
        )


def read_head(transcript: Path) -> tuple[Path | None, str | None]:
    """The first recorded `cwd` and the first timestamp, in file order.

    The first cwd is the folder the session started in. The first line's timestamp is
    when the file was created: resumed sessions copy older records (with their original
    timestamps) after it, so the minimum timestamp would point at the parent session.
    """
    cwd: Path | None = None
    started: str | None = None
    try:
        with open(transcript, "rb") as f:
            for i, raw in enumerate(f):
                if i >= _HEAD_LINES or (cwd and started):
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if cwd is None and isinstance(record.get("cwd"), str) and record["cwd"]:
                    cwd = Path(record["cwd"])
                if started is None and isinstance(record.get("timestamp"), str):
                    started = record["timestamp"]
    except OSError:
        pass
    return cwd, started


def _encoded_prefixes(folder: Path) -> set[str]:
    """Encoded names for the folder as given and with symlinks resolved (/tmp vs /private/tmp)."""
    return {paths.encode_path(folder.absolute()), paths.encode_path(folder.resolve())}


def _is_inside(cwd: Path | None, root: Path) -> bool:
    if cwd is None:
        return False
    return cwd.is_relative_to(root) or cwd.resolve().is_relative_to(root)
