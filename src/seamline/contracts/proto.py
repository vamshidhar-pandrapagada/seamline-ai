"""Read .proto files into contract data: each message's fields, each RPC's request/response.

A small parser for the parts that define contracts (package, messages, fields, services,
rpcs), not a full protobuf grammar. Options, reserved ranges and imports are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_BLOCK = re.compile(r"\b(message|service|enum|oneof)\s+(\w+)\s*\{")
_FIELD = re.compile(
    r"(?:\b(?:repeated|optional|required)\s+)?"
    r"(map\s*<\s*[\w.]+\s*,\s*[\w.]+\s*>|[A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)\s*=\s*\d+"
)
_NOT_TYPES = frozenset({"option", "reserved", "extensions", "import", "syntax", "package"})
_RPC = re.compile(
    r"\brpc\s+(\w+)\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)"
)


@dataclass
class ProtoMessage:
    name: str  # Qualified within the file: Outer.Inner
    line: int
    fields: list[tuple[str, str]] = field(default_factory=list)  # (name, type)


@dataclass
class ProtoRpc:
    service: str
    method: str
    request: str
    response: str
    line: int


@dataclass
class ProtoFile:
    package: str | None
    messages: list[ProtoMessage]
    rpcs: list[ProtoRpc]


def parse_proto(text: str) -> ProtoFile:
    # Blank out comments but keep newlines, so line numbers stay right
    clean = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    pkg = _PACKAGE.search(clean)
    messages: list[ProtoMessage] = []
    rpcs: list[ProtoRpc] = []
    _walk(clean, 0, len(clean), [], messages, rpcs)
    return ProtoFile(pkg.group(1) if pkg else None, messages, rpcs)


def parse_proto_file(path: Path) -> ProtoFile:
    return parse_proto(path.read_text(encoding="utf-8", errors="replace"))


def _walk(text: str, start: int, end: int, outer: list[str], messages, rpcs) -> None:
    pos = start
    while m := _BLOCK.search(text, pos, end):
        kind, name = m.groups()
        body_start = m.end()
        body_end = _matching_brace(text, body_start - 1)
        if kind == "message":
            msg = ProtoMessage(".".join([*outer, name]), text.count("\n", 0, m.start()) + 1)
            msg.fields = _fields(text[body_start:body_end])
            messages.append(msg)
            _walk(text, body_start, body_end, [*outer, name], messages, rpcs)
        elif kind == "service":
            for r in _RPC.finditer(text, body_start, body_end):
                line = text.count("\n", 0, r.start()) + 1
                rpcs.append(ProtoRpc(name, r.group(1), r.group(2), r.group(3), line))
        pos = body_end + 1


def _fields(body: str) -> list[tuple[str, str]]:
    """Fields declared directly in a message body (including inside its oneofs)."""
    flat = _remove_nested(body)
    found = []
    for m in _FIELD.finditer(flat):
        ftype = re.sub(r"\s+", "", m.group(1))
        if ftype not in _NOT_TYPES:
            found.append((m.group(2), ftype))
    return found


def _remove_nested(body: str) -> str:
    """Drop nested message/enum bodies; keep oneof contents (their fields belong here)."""
    out, pos = [], 0
    while m := _BLOCK.search(body, pos):
        out.append(body[pos : m.start()])
        close = _matching_brace(body, m.end() - 1)
        if m.group(1) == "oneof":
            out.append(_remove_nested(body[m.end() : close]))
        pos = close + 1
    out.append(body[pos:])
    return " ".join(out)


def _matching_brace(text: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)
