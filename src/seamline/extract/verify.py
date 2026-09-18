"""Reject any fact whose quote isn't in the transcript: a cheap, strong guard against invention.

"Verbatim" tolerates only formatting: whitespace runs, letter case, Markdown emphasis,
backticks, list/table/heading markers, and quote-mark style. A quote made of pieces joined
by "…" passes only if every piece has at least three words and the pieces appear in order
on the same line. Wording must match exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from seamline.extract.chunker import Chunk
from seamline.extract.schema import ExtractedFact

MIN_QUOTE_CHARS = 12
MIN_PIECE_WORDS = 3

_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.)\s*")
# Line-leading list items, numbered items, headings and block quotes
_LINE_MARKERS = re.compile(r"(?m)^\s*(?:[-*+>]|\d+[.)]|#{1,6})\s+")
_FORMATTING = re.compile(r"[*`|\"'“”‘’]+")  # Not "_": it is part of identifiers like amount_cents
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    line: int | None = None  # Where the quote was actually found


def normalize(text: str) -> str:
    text = _LINE_MARKERS.sub(" ", text)
    text = _FORMATTING.sub("", text)
    return _SPACES.sub(" ", text).strip().lower()


def verify(fact: ExtractedFact, chunk: Chunk) -> Verdict:
    pieces = [normalize(p) for p in _ELLIPSIS.split(fact.quote) if p.strip()]
    if sum(len(p) for p in pieces) < MIN_QUOTE_CHARS:
        return Verdict(False, "quote too short")
    if len(pieces) > 1 and any(len(p.split()) < MIN_PIECE_WORDS for p in pieces):
        return Verdict(False, "quote pieces too short")
    stated = chunk.quotable.get(fact.line)
    if stated is not None and _contains_in_order(normalize(stated), pieces):
        return Verdict(True, line=fact.line)
    # Right words, wrong line number: accept, but record where it really is
    for line, text in chunk.quotable.items():
        if _contains_in_order(normalize(text), pieces):
            return Verdict(True, reason="line corrected", line=line)
    return Verdict(False, "quote not found in transcript")


def _contains_in_order(text: str, pieces: list[str]) -> bool:
    pos = 0
    for piece in pieces:
        found = text.find(piece, pos)
        if found < 0:
            return False
        pos = found + len(piece)
    return True
