"""Mark session facts stale when their service's code no longer backs them.

Sessions only report what was said. When payments later stops reading `amount` and nobody
mentions it, the old assumption would keep its mismatch open forever. So each active
session `provides`/`assumes` fact with typed fields is checked against its service's code:

- stale when the code changed AFTER the fact was stated (a file in the service folder is
  newer than the fact's latest evidence) and a field the fact names no longer appears.
  Code that hasn't changed since is not evidence: the session may describe a contract
  before it's written.
- restored when a fact made stale this way finds all its fields again.

"Appears" means used as a field, not merely as a word: a quoted key (`event['amount']`,
`"amount":`), an attribute (`.amount`), or a declaration (`amount: Decimal`,
`pub amount: i64`, `int64 amount = 2`), in snake, camel or Pascal case. The `amount`
inside `amount_cents` doesn't count. Local, no model calls.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from seamline.config import Config
from seamline.detect import SKIP_DIRS
from seamline.ledger import queries as q
from seamline.resolve.drift import typed_fields
from seamline.resolve.normalize import words

CODE_SUFFIXES = frozenset(
    (
        *(".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs", ".java"),
        *(".kt", ".kts", ".scala", ".rb", ".php", ".cs", ".swift", ".proto", ".graphql"),
        *(".sql", ".yaml", ".yml", ".toml", ".json", ".avsc"),
    )
)
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 5_000


@dataclass
class ServiceCode:
    text: str  # All code files of the service, concatenated
    newest: str | None  # ISO time of the most recently modified file
    files: int


@dataclass
class StalenessResult:
    checked: int = 0
    marked_stale: list[tuple[int, str]] = field(default_factory=list)  # (fact id, why)
    restored: list[int] = field(default_factory=list)
    skipped_unchanged: int = 0  # Fields missing, but code not changed since the statement


_ACRONYMS = frozenset({"id", "url", "uri", "api", "http", "json", "uuid", "ip", "sql"})


def spellings(name: str) -> set[str]:
    """order_id -> order_id, orderId, OrderId, order-id, and Go-style orderID, OrderID."""
    w = words(name)
    if not w:
        return {name}

    def cap(x: str, acronyms: bool) -> str:
        return x.upper() if acronyms and x in _ACRONYMS else x.capitalize()

    out = {"_".join(w), "-".join(w), name}
    for acronyms in (False, True):
        out.add(w[0] + "".join(cap(x, acronyms) for x in w[1:]))
        out.add("".join(cap(x, acronyms) for x in w))
    return out


def field_pattern(name: str) -> re.Pattern:
    alts = "|".join(re.escape(s) for s in sorted(spellings(name), key=len, reverse=True))
    return re.compile(
        # quoted key: event['amount'], "amount":
        rf"""["'`](?:{alts})["'`]"""
        # attribute: event.amount, self.amountCents
        rf"|\.(?:{alts})\b"
        # declaration: amount: Decimal, amount?: number, pub amount: i64
        rf"|(?<![\w.])(?:{alts})\s*[?!]?\s*:(?!:)"
        # typed declaration: int64 amount = 2; String amount;
        rf"|\b[\w.<>,\[\]]+\s+(?:{alts})\s*(?:=\s*\d|;)",
    )


def field_used(name: str, code: str) -> bool:
    return bool(field_pattern(name).search(code))


def read_service_code(folder: Path) -> ServiceCode:
    parts, newest, count = [], 0.0, 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            path = Path(dirpath) / fn
            if path.suffix not in CODE_SUFFIXES:
                continue
            try:
                st = path.stat()
                if st.st_size > MAX_FILE_BYTES:
                    continue
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            count += 1
            if count >= MAX_FILES:
                break
    stamp = datetime.fromtimestamp(newest, tz=UTC).isoformat() if count else None
    return ServiceCode("\n".join(parts), stamp, count)


def refresh(conn: sqlite3.Connection, config: Config) -> StalenessResult:
    result = StalenessResult()
    code: dict[str, ServiceCode] = {}
    for name, folder in config.service_dirs().items():
        if folder.is_dir():
            code[name] = read_service_code(folder)

    for f in q.facts_for_code_check(conn):
        svc = code.get(f.service or "")
        fields = typed_fields(f.details)
        if not svc or not svc.files or not fields:
            continue
        result.checked += 1
        # Search by the normalized key: stored names can carry a stray "field_" prefix
        missing = [
            display for key, (display, _r, _t) in fields.items() if not field_used(key, svc.text)
        ]
        stated = q.latest_evidence_time(conn, f.id)
        if f.status == "active" and missing:
            if not _changed_after(svc.newest, stated):
                result.skipped_unchanged += 1
                continue
            why = f"{f.service} code no longer uses " + ", ".join(f"`{m}`" for m in missing)
            q.mark_stale(conn, f.id, "code", why)
            q.log_change(conn, "fact_stale", [f.service], fact_id=f.id)
            result.marked_stale.append((f.id, why))
        elif f.status == "stale" and not missing:
            q.restore(conn, f.id)
            q.log_change(conn, "fact_restored", [f.service], fact_id=f.id)
            result.restored.append(f.id)
    return result


def _changed_after(newest: str | None, stated: str | None) -> bool:
    if not newest or not stated:
        return False
    try:
        return _parse(newest) > _parse(stated)
    except ValueError:
        return False


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
