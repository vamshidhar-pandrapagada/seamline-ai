"""Typed transcript events. All knowledge of Claude Code's JSONL record shapes lives here.

One JSONL line can hold several content blocks, so one line becomes one or more events.
See docs/transcript-format.md for the format this is written against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    USER_PROMPT = "user_prompt"  # Something the user typed
    ASSISTANT_TEXT = "assistant_text"  # What Claude said
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    COMPACT_SUMMARY = "compact_summary"  # Claude's summary after /compact or auto-compaction
    META = "meta"  # Injected by Claude Code (skill bodies, caveats, slash-command wrappers)
    INTERRUPT = "interrupt"  # "[Request interrupted by user]"
    SYSTEM = "system"  # system records: hook summaries, compact boundaries
    ATTACHMENT = "attachment"  # Reminders, context deltas, hook output
    OTHER = "other"  # Titles, queue operations, file-history snapshots, …


# Tools whose input names a file the turn touched: the anchors for fact attribution.
FILE_TOOLS = {
    "Read": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

_COMMAND_TAGS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<task-notification>",
)
_INTERRUPT_PREFIX = "[Request interrupted by user"


@dataclass(frozen=True)
class Event:
    line_no: int  # 1-based line in the transcript file
    block: int  # Index of the content block within the line
    kind: Kind
    text: str = ""  # Display text: prompt, reply, tool summary or result
    uuid: str | None = None
    session_id: str | None = None
    timestamp: str | None = None  # ISO 8601, as recorded
    cwd: str | None = None
    is_sidechain: bool = False
    record_type: str = ""  # The line's top-level "type"
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)

    @property
    def file_path(self) -> str | None:
        """The file a file-touching tool call names, if any."""
        key = FILE_TOOLS.get(self.tool_name or "")
        value = self.tool_input.get(key) if key else None
        return value if isinstance(value, str) else None


def parse_record(record: dict[str, Any], line_no: int) -> list[Event]:
    """Turn one decoded JSONL record into events."""
    rtype = record.get("type") if isinstance(record.get("type"), str) else ""
    base = {
        "line_no": line_no,
        "uuid": record.get("uuid"),
        "session_id": record.get("sessionId"),
        "timestamp": record.get("timestamp"),
        "cwd": record.get("cwd"),
        "is_sidechain": bool(record.get("isSidechain")),
        "record_type": rtype,
    }
    if rtype == "user":
        return _parse_user(record, base)
    if rtype == "assistant":
        return _parse_assistant(record, base)
    if rtype == "system":
        text = record.get("content") or record.get("subtype") or ""
        return [Event(block=0, kind=Kind.SYSTEM, text=str(text), **base)]
    if rtype == "attachment":
        att = record.get("attachment")
        text = att.get("type", "") if isinstance(att, dict) else ""
        return [Event(block=0, kind=Kind.ATTACHMENT, text=text, **base)]
    return [Event(block=0, kind=Kind.OTHER, text=rtype, **base)]


def _parse_user(record: dict, base: dict) -> list[Event]:
    content = (record.get("message") or {}).get("content")
    if record.get("isCompactSummary"):
        return [Event(block=0, kind=Kind.COMPACT_SUMMARY, text=_as_text(content), **base)]
    if record.get("isMeta"):
        return [Event(block=0, kind=Kind.META, text=_as_text(content), **base)]
    if isinstance(content, str):
        return [Event(block=0, kind=_user_text_kind(content), text=content, **base)]
    if not isinstance(content, list):
        return [Event(block=0, kind=Kind.OTHER, text="", **base)]

    events = []
    for i, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            events.append(
                Event(
                    block=i,
                    kind=Kind.TOOL_RESULT,
                    text=_as_text(block.get("content")),
                    tool_use_id=block.get("tool_use_id"),
                    **base,
                )
            )
        elif btype == "text":
            text = block.get("text", "")
            events.append(Event(block=i, kind=_user_text_kind(text), text=text, **base))
        else:  # images, documents
            events.append(Event(block=i, kind=Kind.OTHER, text=f"[{btype}]", **base))
    return events


def _parse_assistant(record: dict, base: dict) -> list[Event]:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return [Event(block=0, kind=Kind.ASSISTANT_TEXT, text=content, **base)]
    events = []
    for i, block in enumerate(content if isinstance(content, list) else []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            events.append(
                Event(block=i, kind=Kind.ASSISTANT_TEXT, text=block.get("text", ""), **base)
            )
        elif btype == "thinking":
            events.append(
                Event(block=i, kind=Kind.THINKING, text=block.get("thinking", ""), **base)
            )
        elif btype == "tool_use":
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            name = block.get("name", "")
            events.append(
                Event(
                    block=i,
                    kind=Kind.TOOL_USE,
                    text=_summarize_tool(name, tool_input),
                    tool_name=name,
                    tool_use_id=block.get("id"),
                    tool_input=tool_input,
                    **base,
                )
            )
        else:  # redacted_thinking, server tool blocks, …
            events.append(Event(block=i, kind=Kind.OTHER, text=f"[{btype}]", **base))
    return events


def _user_text_kind(text: str) -> Kind:
    stripped = text.lstrip()
    if stripped.startswith(_INTERRUPT_PREFIX):
        return Kind.INTERRUPT
    if stripped.startswith(_COMMAND_TAGS):
        return Kind.META
    return Kind.USER_PROMPT


def _summarize_tool(name: str, tool_input: dict) -> str:
    for key in ("file_path", "notebook_path", "command", "pattern", "url", "query", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _as_text(content: Any) -> str:
    """Flatten message content (string or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "type" in block:
                    parts.append(f"[{block['type']}]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""
