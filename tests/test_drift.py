import sys
from pathlib import Path

import pytest

from seamline.ledger import queries as q
from seamline.ledger.db import connect
from seamline.resolve.drift import recompute
from seamline.resolve.matcher import interface_id_for
from seamline.resolve.supersede import Candidate, store_fact

sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))
import score_drift  # noqa: E402


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def put(
    conn,
    kind,
    service,
    details,
    *,
    origin="session",
    ts="2026-09-01T00:00:00Z",
    name="order.created",
):
    return store_fact(
        conn,
        Candidate(
            kind=kind,
            origin=origin,
            service=service,
            attributed_by="code" if origin == "code" else "folder",
            interface_id=interface_id_for(conn, name, "event"),
            claim=f"{service} {kind} {details}",
            details=[{"name": k, "value": v} for k, v in details.items()],
            confidence=0.9,
            quote="q",
            session_id=None if origin == "code" else service,
            file_path="proto/shop.proto" if origin == "code" else None,
            line_no=1,
            timestamp=ts,
        ),
    ).fact_id


def test_planted_cases_all_right():
    total, right, wrong, known = score_drift.score()
    assert wrong == [] and right == total >= 22
    assert len(known) == 2  # if a fix makes one pass, move it out of known_limitation


def test_mismatch_description_names_both_sides(conn):
    put(conn, "provides", "orders", {"amount_cents": "integer"})
    put(conn, "assumes", "payments", {"amount": "decimal"})
    recompute(conn)
    (m,) = q.open_mismatches(conn)
    assert m["interface"] == "order.created"
    assert m["field"] == "amount"
    assert "payments assumes amount" in m["description"]
    assert "orders doesn't provide it (it has `amount_cents`)" in m["description"]


def test_mismatch_resolves_when_consumer_fixes_assumption_and_is_idempotent(conn):
    put(conn, "provides", "orders", {"amount_cents": "integer"})
    put(conn, "assumes", "payments", {"amount": "decimal"})
    assert len(recompute(conn).opened) == 1
    assert recompute(conn).opened == []  # nothing new on a second run
    put(
        conn,
        "assumes",
        "payments",
        {"amount_cents": "integer"},  # fixed to read amount_cents: the rename supersedes
        ts="2026-09-02T00:00:00Z",
    )
    result = recompute(conn)
    assert len(result.resolved) == 1 and q.open_mismatches(conn) == []
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM changes")]
    assert "mismatch_opened" in kinds and "mismatch_resolved" in kinds


def test_code_facts_are_providers_regardless_of_service(conn):
    put(conn, "provides", None, {"amount_cents": "int64"}, origin="code", name="OrderCreated")
    put(conn, "assumes", "payments", {"amount": "decimal"})
    recompute(conn)
    (m,) = q.open_mismatches(conn)
    assert "the code doesn't provide it" in m["description"]


def test_assumptions_on_other_interfaces_are_ignored(conn):
    put(conn, "provides", "orders", {"amount_cents": "integer"})
    put(conn, "assumes", "payments", {"amount": "decimal"}, name="order.refunded")
    assert recompute(conn).opened == []
