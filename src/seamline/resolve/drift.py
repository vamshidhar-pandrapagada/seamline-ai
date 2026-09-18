"""Contract drift: a consumer's assumption that disagrees with what the provider says.

For each interface, every active `assumes` fact is compared with every active `provides`
fact from a different service (code facts count as providers regardless of service, and
are ground truth). Two disagreements are flagged:

- missing field: the consumer assumes a typed field the provider's (typed) fields don't
  include ("payments assumes amount: decimal; orders provides amount_cents: integer")
- type conflict: both name the same field with clearly different types

Only fields whose value names a type take part, so free-form details ("unit = dollars",
"reason = …") never raise alarms. Mismatches are recomputed from scratch each time: new
ones open, ones that no longer hold (a fact was superseded or fixed) resolve.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from seamline.ledger import queries as q
from seamline.resolve.normalize import field_key, type_key, words


@dataclass(frozen=True)
class Finding:
    interface_id: int
    provides_fact: int
    assumes_fact: int
    field: str
    description: str

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.provides_fact, self.assumes_fact, self.field)


@dataclass
class DriftResult:
    opened: list[int]
    resolved: list[int]


def typed_fields(details: list[dict]) -> dict[str, tuple[str, str, str]]:
    """field key -> (display name, raw value, type) for details whose value names a type."""
    out = {}
    for d in details:
        t = type_key(d.get("value", ""))
        if t and d.get("name"):
            out[field_key(d["name"])] = (d["name"], d["value"], t)
    return out


def compare(provides: q.FactRow, assumes: q.FactRow) -> list[Finding]:
    offered = typed_fields(provides.details)
    wanted = typed_fields(assumes.details)
    if not offered or not wanted:
        return []
    owner = provides.service or ("the code" if provides.origin == "code" else "the provider")
    findings = []
    for key, (name, raw, wanted_type) in wanted.items():
        if key in offered:
            o_name, o_raw, o_type = offered[key]
            if o_type != wanted_type:
                findings.append(
                    Finding(
                        provides.interface_id,
                        provides.id,
                        assumes.id,
                        name,
                        f"`{name}` type: {assumes.service or 'a consumer'} assumes {raw}, "
                        f"{owner} provides {o_raw}",
                    )
                )
            continue
        hint = _closest(key, offered)
        suffix = f" (it has `{hint}`)" if hint else ""
        findings.append(
            Finding(
                provides.interface_id,
                provides.id,
                assumes.id,
                name,
                f"`{name}`: {assumes.service or 'a consumer'} assumes {name} ({raw}), "
                f"but {owner} doesn't provide it{suffix}",
            )
        )
    return findings


def detect(conn: sqlite3.Connection) -> list[Finding]:
    findings: list[Finding] = []
    assumes_by_interface: dict[int, list[q.FactRow]] = {}
    for a in q.list_facts(conn, kind="assumes"):
        if a.interface_id is not None:
            assumes_by_interface.setdefault(a.interface_id, []).append(a)
    for interface_id, assumptions in assumes_by_interface.items():
        providers = q.list_facts(conn, kind="provides", interface_id=interface_id)
        for p in providers:
            for a in assumptions:
                if p.origin != "code" and p.service == a.service:
                    continue  # A service's assumption about itself isn't drift
                findings.extend(compare(p, a))
    return findings


def recompute(conn: sqlite3.Connection) -> DriftResult:
    current = {f.key: f for f in detect(conn)}
    existing = {
        (r["provides_fact"], r["assumes_fact"], r["field"]): r
        for r in conn.execute("SELECT * FROM mismatches WHERE status = 'open'")
    }
    opened, resolved = [], []
    for key, f in current.items():
        if key in existing:
            continue
        row = conn.execute(
            "SELECT id FROM mismatches WHERE provides_fact = ? AND assumes_fact = ? AND field = ?",
            key,
        ).fetchone()
        if row:  # Seen before and resolved, now back: reopen
            conn.execute(
                "UPDATE mismatches SET status = 'open', resolved_at = NULL, description = ? "
                "WHERE id = ?",
                (f.description, row["id"]),
            )
            mismatch_id = row["id"]
        else:
            mismatch_id = conn.execute(
                """INSERT INTO mismatches(interface_id, provides_fact, assumes_fact, field,
                                          description) VALUES (?, ?, ?, ?, ?)""",
                (f.interface_id, f.provides_fact, f.assumes_fact, f.field, f.description),
            ).lastrowid
        opened.append(mismatch_id)
        q.log_change(conn, "mismatch_opened", _services(conn, f), mismatch_id=mismatch_id)
    for key, row in existing.items():
        if key not in current:
            conn.execute(
                "UPDATE mismatches SET status = 'resolved', "
                "resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (row["id"],),
            )
            resolved.append(row["id"])
            q.log_change(
                conn,
                "mismatch_resolved",
                [
                    q.get_fact(conn, row["provides_fact"]).service,
                    q.get_fact(conn, row["assumes_fact"]).service,
                ],
                mismatch_id=row["id"],
            )
    return DriftResult(opened, resolved)


def _services(conn: sqlite3.Connection, f: Finding) -> list[str | None]:
    return [q.get_fact(conn, f.provides_fact).service, q.get_fact(conn, f.assumes_fact).service]


def _closest(key: str, offered: dict) -> str | None:
    """A provided field sharing a word with the missing one: amount -> amount_cents."""
    want = set(words(key))
    best = max(offered, key=lambda k: len(want & set(words(k))), default=None)
    return offered[best][0] if best and want & set(words(best)) else None
