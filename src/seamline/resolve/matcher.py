"""Is this the same interface, or the same subject, as something already in the ledger?

Rules only for now. The plan's LLM matcher for ambiguous cases waits until real data shows
where rules fall short.
"""

from __future__ import annotations

import re
import sqlite3

from seamline.ledger import queries as q
from seamline.resolve.normalize import field_key, interface_key

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "from",
        "as",
        "at",
        "is",
        "are",
        "be",
        "was",
        "were",
        "will",
        "would",
        "should",
        "must",
        "can",
        "could",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "into",
        "than",
        "then",
        "so",
        "not",
        "no",
        "use",
        "uses",
        "using",
    ]
)


def find_interface(conn: sqlite3.Connection, name: str) -> int | None:
    """An existing interface for this name: exact spelling, normalized key, or a known
    alias with the same key (so `agents.v1.AgentInfo` finds the proto's `AgentInfo`)."""
    alias = name.strip()
    key = interface_key(alias)
    row = (
        q.interface_by_alias(conn, alias)
        or q.interface_by_key(conn, key)
        or q.interface_by_alias(conn, f"key:{key}")
    )
    return row["id"] if row else None


def interface_id_for(conn: sqlite3.Connection, name: str, kind: str | None) -> int:
    """Find or create the interface a name refers to, recording the spelling as an alias."""
    alias = name.strip()
    interface_id = find_interface(conn, alias)
    if interface_id is None:
        interface_id = q.create_interface(conn, interface_key(alias), alias, kind)
    elif kind:
        q.set_interface_kind_if_missing(conn, interface_id, kind)
    add_spelling(conn, alias, interface_id)
    return interface_id


def add_spelling(conn: sqlite3.Connection, name: str, interface_id: int) -> None:
    """Record a spelling (and its normalized key) as naming this interface."""
    q.add_alias(conn, name.strip(), interface_id)
    q.add_alias(conn, f"key:{interface_key(name)}", interface_id)


def merge_interfaces(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    """Fold interface `drop` into `keep`: its facts, spellings and mismatches move over."""
    if keep == drop:
        return
    conn.execute("UPDATE facts SET interface_id = ? WHERE interface_id = ?", (keep, drop))
    conn.execute("UPDATE mismatches SET interface_id = ? WHERE interface_id = ?", (keep, drop))
    conn.execute(
        "UPDATE OR IGNORE interface_aliases SET interface_id = ? WHERE interface_id = ?",
        (keep, drop),
    )
    conn.execute("DELETE FROM interface_aliases WHERE interface_id = ?", (drop,))
    conn.execute("DELETE FROM interfaces WHERE id = ?", (drop,))


def claim_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Word-set Jaccard similarity of two claims (0..1)."""
    wa, wb = claim_words(a), claim_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def detail_fields(details: list[dict]) -> set[str]:
    return {field_key(d["name"]) for d in details if d.get("name")}


# Words too common in field names to suggest a rename (created_at / updated_at share "at")
GENERIC_FIELD_WORDS = frozenset(
    [
        "id",
        "ids",
        "at",
        "on",
        "by",
        "is",
        "has",
        "type",
        "kind",
        "name",
        "status",
        "state",
        "count",
        "num",
        "key",
        "value",
        "data",
        "info",
        "code",
        "time",
        "date",
        "ts",
        "url",
        "uri",
    ]
)


def fields_related(a: set[str], b: set[str]) -> bool:
    """Same field, or a likely rename sharing a meaningful word: amount / amount_cents.

    Only the fallback when no judge is available: word overlap can't tell a rename from two
    different fields (order_id / order_status share "order"), and misses renames that share
    no word (amount / total_cents)."""
    if a & b:
        return True
    words_a = {w for f in a for w in f.split("_")} - GENERIC_FIELD_WORDS
    words_b = {w for f in b for w in f.split("_")} - GENERIC_FIELD_WORDS
    return bool(words_a & words_b)


def same_details(a: list[dict], b: list[dict]) -> bool:
    def norm(ds):
        return {(field_key(d["name"]), " ".join(d["value"].lower().split())) for d in ds}

    return norm(a) == norm(b)
