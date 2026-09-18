"""Read a transcript incrementally from a saved byte offset, redacting as we go.

Only complete lines are consumed: Claude Code may be mid-write on the last one, so a
partial line is left for the next read and the returned offset stops before it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from seamline.transcripts.models import Event, Kind, parse_record
from seamline.transcripts.redact import MASK, is_env_file, redact, redact_value


@dataclass
class ReadResult:
    events: list[Event]
    offset: int  # Byte offset to resume from
    line_no: int  # Number of complete lines consumed so far
    bad_lines: list[int] = field(default_factory=list)  # Lines that weren't valid JSON


def read_events(path: Path | str, offset: int = 0, line_no: int = 0) -> ReadResult:
    """Parse complete lines after `offset`. `line_no` is the count of lines before it."""
    events: list[Event] = []
    bad: list[int] = []
    env_tool_ids: set[str] = set()  # Read calls on .env files; their results are masked
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # Partial line still being written
            offset += len(raw)
            line_no += 1
            try:
                record = json.loads(raw)
            except ValueError:
                bad.append(line_no)
                continue
            if not isinstance(record, dict):
                bad.append(line_no)
                continue
            for event in parse_record(record, line_no):
                if event.kind is Kind.TOOL_USE and is_env_file(event.file_path):
                    env_tool_ids.add(event.tool_use_id or "")
                if event.kind is Kind.TOOL_RESULT and event.tool_use_id in env_tool_ids:
                    events.append(replace(event, text=MASK))
                else:
                    events.append(_redacted(event))
    return ReadResult(events=events, offset=offset, line_no=line_no, bad_lines=bad)


def _redacted(event: Event) -> Event:
    return replace(event, text=redact(event.text), tool_input=redact_value(event.tool_input))
