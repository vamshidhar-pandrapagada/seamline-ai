"""The SessionSource interface: the only thing that knows where an IDE keeps its sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from seamline.transcripts.reader import ReadResult


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    path: Path  # The transcript file
    start_cwd: Path | None  # Folder the session started in (first recorded cwd)
    started: str | None  # First timestamp in file order (copied history keeps older ones)
    size: int  # Bytes
    last_activity: datetime  # File modification time
    subagents: int = 0  # Sidechain transcripts stored next to it


class SessionSource(Protocol):
    name: str

    def list_sessions(self, under: Path | None = None) -> list[SessionInfo]:
        """Sessions whose start folder is `under` or inside it (all sessions if None)."""
        ...

    def read(self, session: SessionInfo, offset: int = 0, line_no: int = 0) -> ReadResult:
        """New events after a saved position."""
        ...
