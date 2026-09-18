"""Turn a session's labeled events into model-sized excerpts, split on turn boundaries.

Only `keep` events (the developer's and Claude's words) can be quoted. `anchor` events
appear as context lines naming the files a turn touched. Evidence and skipped events are
left out: tool output never reaches the extractor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from seamline.transcripts.classify import Label
from seamline.transcripts.models import Event, Kind

CHARS_PER_TOKEN = 4  # Rough, and on the safe side for English and code
DEFAULT_BUDGET_TOKENS = 6000

_VERBS = {
    "Read": "read",
    "Write": "wrote",
    "Edit": "edited",
    "MultiEdit": "edited",
    "NotebookEdit": "edited",
}


@dataclass
class Chunk:
    text: str
    quotable: dict[int, str] = field(default_factory=dict)  # line -> full USER/CLAUDE text
    events: list[Event] = field(default_factory=list)  # The keep events it contains

    @property
    def lines(self) -> tuple[int, int]:
        nums = [e.line_no for e in self.events] or [0]
        return min(nums), max(nums)


def build_chunks(
    labeled: list[tuple[Event, Label]],
    root: Path | None = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> list[Chunk]:
    budget = budget_tokens * CHARS_PER_TOKEN
    turns = _turns([(e, lbl) for e, lbl in labeled if lbl in (Label.KEEP, Label.ANCHOR)])

    chunks: list[Chunk] = []
    current: list[tuple[str, Event, Label]] = []
    size = 0
    for turn in turns:
        rendered = [(piece, e, lbl) for e, lbl in turn for piece in _render(e, lbl, root, budget)]
        turn_size = sum(len(p) + 1 for p, _, _ in rendered)
        if current and size + turn_size > budget:
            chunks.append(_make_chunk(current))
            current, size = [], 0
        for piece, e, lbl in rendered:  # A single turn larger than the budget is split
            if current and size + len(piece) + 1 > budget:
                chunks.append(_make_chunk(current))
                current, size = [], 0
            current.append((piece, e, lbl))
            size += len(piece) + 1
    if current:
        chunks.append(_make_chunk(current))
    # Drop excerpts with nothing quotable (e.g. only file anchors)
    return [c for c in chunks if c.quotable]


def _turns(labeled: list[tuple[Event, Label]]) -> list[list[tuple[Event, Label]]]:
    """A turn starts at each thing the developer typed."""
    turns: list[list[tuple[Event, Label]]] = []
    for e, lbl in labeled:
        if e.kind is Kind.USER_PROMPT or not turns:
            turns.append([])
        turns[-1].append((e, lbl))
    return turns


def _render(event: Event, label: Label, root: Path | None, budget: int) -> list[str]:
    tag = f"[L{event.line_no}]"
    if label is Label.ANCHOR:
        verb = _VERBS.get(event.tool_name or "", "touched")
        return [f"{tag} (Claude {verb} {_relative(event.file_path, root)})"]
    who = "USER" if event.kind is Kind.USER_PROMPT else "CLAUDE"
    prefix = f"{tag} {who}: "
    text = event.text.strip()
    room = max(budget - len(prefix) - 1, 200)
    # An event longer than a whole excerpt is split; each piece keeps the same line tag
    return [prefix + text[i : i + room] for i in range(0, len(text), room)] or [prefix]


def _make_chunk(items: list[tuple[str, Event, Label]]) -> Chunk:
    quotable: dict[int, str] = {}
    events: list[Event] = []
    for _, e, lbl in items:
        if lbl is Label.KEEP:
            quotable[e.line_no] = (
                quotable[e.line_no] + "\n" + e.text if e.line_no in quotable else e.text
            )
            if e not in events:
                events.append(e)
    return Chunk(text="\n".join(p for p, _, _ in items), quotable=quotable, events=events)


def _relative(path: str | None, root: Path | None) -> str:
    if not path:
        return "?"
    if root:
        try:
            return str(Path(path).relative_to(root))
        except ValueError:
            pass
    return path
