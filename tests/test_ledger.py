"""Ledger storage: migrations, storing facts, duplicates, superseding."""

import sqlite3

import pytest

from seamline.ledger import queries as q
from seamline.ledger.db import connect, migrations
from seamline.resolve.matcher import interface_id_for
from seamline.resolve.supersede import Candidate, store_fact


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "ledger.db")
    yield c
    c.close()


def cand(conn, **kw):
    base = dict(
        kind="provides",
        origin="session",
        service="orders",
        attributed_by="folder",
        interface_id=interface_id_for(conn, "order.created", "event"),
        claim="orders emits order.created with amount_cents as an integer.",
        details=[{"name": "amount_cents", "value": "integer"}],
        confidence=0.9,
        quote="emit order.created with amount_cents",
        session_id="s1",
        line_no=3,
        timestamp="2026-09-01T10:00:00Z",
    )
    return Candidate(**{**base, **kw})


def test_migrations_apply_once(tmp_path):
    path = tmp_path / "ledger.db"
    connect(path).close()
    c = connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == migrations()[-1][0]
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_check_constraints_enforce_vocabulary(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO facts(kind, origin, attributed_by, claim) "
            "VALUES ('finding', 'session', 'none', 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO interfaces(key, name, kind) VALUES ('k', 'k', 'rest')")


def test_interface_spellings_share_one_row(conn):
    a = interface_id_for(conn, "order.created", "event")
    b = interface_id_for(conn, "OrderCreated", None)
    c = interface_id_for(conn, "ORDER_CREATED", None)
    assert a == b == c
    aliases = {
        r[0]
        for r in conn.execute("SELECT alias FROM interface_aliases")
        if not r[0].startswith("key:")
    }
    assert aliases == {"order.created", "OrderCreated", "ORDER_CREATED"}


def test_duplicate_adds_evidence_not_a_fact(conn):
    first = store_fact(conn, cand(conn))
    again = store_fact(
        conn,
        cand(
            conn,
            session_id="s2",
            line_no=40,
            quote="amount_cents again",
            timestamp="2026-09-02T10:00:00Z",
        ),
    )
    assert again.outcome == "duplicate" and again.fact_id == first.fact_id
    assert len(q.evidence_for(conn, first.fact_id)) == 2
    assert len(q.list_facts(conn)) == 1


def test_newer_contract_supersedes_older(conn):
    old = store_fact(conn, cand(conn))
    new = store_fact(
        conn,
        cand(
            conn,
            claim="orders now sends amount_cents as a string.",
            details=[{"name": "amount_cents", "value": "string"}],
            timestamp="2026-09-03T10:00:00Z",
            line_no=90,
        ),
    )
    assert new.outcome == "superseded_existing" and new.replaced == [old.fact_id]
    assert q.get_fact(conn, old.fact_id).status == "superseded"
    assert q.superseded_by(conn, old.fact_id) == new.fact_id
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM changes ORDER BY change_id")]
    assert kinds == ["fact_added", "fact_added", "fact_superseded"]


def test_different_aspects_of_one_interface_coexist(conn):
    store_fact(conn, cand(conn))
    store_fact(
        conn,
        cand(
            conn,
            claim="order.created carries currency as a string.",
            details=[{"name": "currency", "value": "string"}],
        ),
    )
    assert len(q.list_facts(conn)) == 2


def test_out_of_order_backfill_stores_older_fact_as_superseded(conn):
    current = store_fact(conn, cand(conn, timestamp="2026-09-05T00:00:00Z"))
    older = store_fact(
        conn,
        cand(
            conn,
            details=[{"name": "amount_cents", "value": "string"}],
            claim="orders sends amount_cents as a string.",
            timestamp="2026-09-01T00:00:00Z",
            line_no=7,
        ),
    )
    assert older.outcome == "stored_as_older"
    assert q.get_fact(conn, current.fact_id).status == "active"
    assert q.get_fact(conn, older.fact_id).status == "superseded"


def test_decisions_supersede_by_similar_claims(conn):
    d = dict(kind="decision", interface_id=None, details=[])
    a = store_fact(conn, cand(conn, **d, claim="Use Redis streams between orders and payments."))
    b = store_fact(
        conn,
        cand(
            conn,
            **d,
            claim="Use Kafka streams between orders and payments.",
            timestamp="2026-09-02T00:00:00Z",
        ),
    )
    c = store_fact(
        conn,
        cand(
            conn, **d, claim="Retry failed charges three times.", timestamp="2026-09-03T00:00:00Z"
        ),
    )
    assert b.replaced == [a.fact_id]
    assert c.outcome == "added"


def test_session_and_code_facts_never_replace_each_other(conn):
    code = store_fact(
        conn,
        cand(
            conn,
            origin="code",
            attributed_by="code",
            details=[{"name": "amount_cents", "value": "int64"}],
        ),
    )
    session = store_fact(
        conn,
        cand(
            conn,
            details=[{"name": "amount_cents", "value": "string"}],
            timestamp="2026-09-09T00:00:00Z",
        ),
    )
    assert session.outcome == "added"
    assert q.get_fact(conn, code.fact_id).status == "active"


def test_fts_search(conn):
    store_fact(conn, cand(conn))
    hits = conn.execute(
        "SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'amount_cents'"
    ).fetchall()
    assert len(hits) == 1


def test_renamed_field_supersedes(conn):
    old = store_fact(
        conn,
        cand(
            conn,
            kind="assumes",
            service="payments",
            details=[{"name": "amount", "value": "decimal"}],
        ),
    )
    new = store_fact(
        conn,
        cand(
            conn,
            kind="assumes",
            service="payments",
            details=[{"name": "amount_cents", "value": "integer"}],
            timestamp="2026-09-02T00:00:00Z",
        ),
    )
    assert new.replaced == [old.fact_id]
