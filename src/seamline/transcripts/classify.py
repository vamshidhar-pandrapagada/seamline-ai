"""Label each event by what it can contribute to the ledger.

- keep:     can create facts (the user's words, Claude's replies)
- anchor:   names a file the conversation touched; used to attribute facts to services
- evidence: supports or checks facts but never creates them (tool calls and results,
            compaction summaries). Tool output can contain planted instructions, so it
            never becomes a fact on its own.
- skip:     noise (thinking, reminders, titles, hook summaries, interrupt markers)

Events whose uuid already appeared in an earlier session are `inherited`: history copied
into a resumed or forked session. They are skipped so facts aren't counted twice.
"""

from __future__ import annotations

from enum import StrEnum

from seamline.transcripts.models import Event, Kind


class Label(StrEnum):
    KEEP = "keep"
    ANCHOR = "anchor"
    EVIDENCE = "evidence"
    SKIP = "skip"


_BY_KIND = {
    Kind.USER_PROMPT: Label.KEEP,
    Kind.ASSISTANT_TEXT: Label.KEEP,
    Kind.TOOL_RESULT: Label.EVIDENCE,
    Kind.COMPACT_SUMMARY: Label.EVIDENCE,
    Kind.THINKING: Label.SKIP,
    Kind.META: Label.SKIP,
    Kind.INTERRUPT: Label.SKIP,
    Kind.SYSTEM: Label.SKIP,
    Kind.ATTACHMENT: Label.SKIP,
    Kind.OTHER: Label.SKIP,
}


def classify(event: Event, inherited: frozenset[str] | set[str] = frozenset()) -> Label:
    if event.uuid and event.uuid in inherited:
        return Label.SKIP
    if event.is_sidechain:
        # Subagent turns: their prompts are written by Claude, not the user.
        return Label.SKIP if event.kind is not Kind.TOOL_USE else _tool_label(event)
    if event.kind is Kind.TOOL_USE:
        return _tool_label(event)
    if event.kind in (Kind.USER_PROMPT, Kind.ASSISTANT_TEXT) and not event.text.strip():
        return Label.SKIP
    return _BY_KIND[event.kind]


def _tool_label(event: Event) -> Label:
    return Label.ANCHOR if event.file_path else Label.EVIDENCE
