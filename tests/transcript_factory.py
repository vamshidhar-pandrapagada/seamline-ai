"""Write small synthetic Claude Code transcripts into a fake ~/.claude for tests."""

from __future__ import annotations

import json
from pathlib import Path

from seamline.paths import encode_path


def write_session(
    home: Path,
    cwd: Path | str,
    session_id: str,
    records: list[dict],
    *,
    started: str = "2026-09-01T10:00:00.000Z",
    folder: str | None = None,
) -> Path:
    """A transcript whose first line (a queue-operation, like the real thing) sets `started`."""
    cwd = str(cwd)
    project_dir = home / "projects" / (folder or encode_path(cwd))
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "sessionId": session_id,
            "timestamp": started,
        }
    ]
    for r in records:
        lines.append({"sessionId": session_id, "cwd": cwd, "timestamp": started, **r})
    path = project_dir / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def prompt(uuid: str, text: str) -> dict:
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def reply(uuid: str, text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
