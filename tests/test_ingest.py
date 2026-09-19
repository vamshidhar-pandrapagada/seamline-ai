"""End to end with a fake model: sessions in a project → ledger → facts, attribution, drift."""

import pytest
from shop_project import model

from seamline.cli import main
from seamline.extract.providers import FakeProvider
from seamline.ledger import queries as q
from seamline.ledger.db import connect
from seamline.ledger_views import run_ingest


def test_ingest_attributes_stores_and_detects_drift(shop):
    conn = connect(shop.root / ".seamline" / "ledger.db")
    lines = []
    status = run_ingest(conn, shop, lambda: FakeProvider(model), yes=True, out=lines.append)
    assert status == 0
    facts = {f.kind: f for f in q.list_facts(conn)}
    assert (facts["provides"].service, facts["provides"].attributed_by) == (
        "orders",
        "name",
    )  # the fact text names orders
    # Root-level session, but the turn edited payments files: lands on payments (Phase 3 gate)
    assert (facts["assumes"].service, facts["assumes"].attributed_by) == ("payments", "files")
    (m,) = q.open_mismatches(conn)
    assert m["field"] == "amount"
    text = "\n".join(lines)
    assert "OPEN MISMATCHES (1)" in text and "(it has `amount_cents`)" in text

    # Offsets advanced: a second ingest has nothing to do and calls no model
    lines.clear()
    assert (
        run_ingest(conn, shop, lambda: pytest.fail("model called"), yes=True, out=lines.append) == 0
    )
    assert "Nothing to ingest" in lines[0]


def test_ingest_asks_before_spending(shop):
    conn = connect(":memory:")
    lines = []
    assert (
        run_ingest(
            conn, shop, lambda: pytest.fail("model called"), ask=lambda _: "n", out=lines.append
        )
        == 1
    )
    assert "about $" in lines[0]
    assert "Cancelled; nothing sent." in lines
    assert q.all_session_states(conn) == {}


def test_failed_excerpt_keeps_offset_for_retry(shop):
    from seamline.extract.providers import ProviderError

    conn = connect(":memory:")
    run_ingest(
        conn, shop, lambda: FakeProvider([ProviderError("down")] * 5), yes=True, out=lambda _: None
    )
    assert q.all_session_states(conn) == {}  # nothing marked as read


def test_cli_ledger_commands(shop, monkeypatch, capsys):
    monkeypatch.setattr("seamline.cli.make_provider", lambda *a, **k: FakeProvider(model))
    root = str(shop.root)
    assert main(["ingest", "--root", root, "--yes"]) == 0
    assert main(["facts", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "PROVIDES (1)" in out and "orders-1 L2" in out
    assert main(["facts", "--root", root, "--service", "payments"]) == 0
    assert "ASSUMES (1)" in capsys.readouterr().out
    assert main(["drift", "--root", root]) == 1  # open drift → non-zero
    assert "amount" in capsys.readouterr().out
    assert main(["scan", "--root", root]) == 0
    assert "No [contracts]" in capsys.readouterr().out


def test_ledger_commands_require_a_project(tmp_path, capsys):
    assert main(["facts", "--root", str(tmp_path)]) == 1
    assert "run `seamline init`" in capsys.readouterr().err
    assert not (tmp_path / ".seamline").exists()


def test_redo_reextracts_and_cancel_changes_nothing(shop):
    conn = connect(":memory:")
    run_ingest(conn, shop, lambda: FakeProvider(model), yes=True, out=lambda _: None)
    before = {f.id for f in q.list_facts(conn)}

    # Cancelled redo: nothing removed
    run_ingest(
        conn,
        shop,
        lambda: pytest.fail("model called"),
        session_id="orders-1",
        redo=True,
        ask=lambda _: "n",
        out=lambda _: None,
    )
    assert {f.id for f in q.list_facts(conn)} == before

    # Redo with a model that now reports a string type: old fact gone, new one in
    def changed(prompt_text):
        out = model(prompt_text)
        for f in out["facts"]:
            f["details"] = [{"name": "amount_cents", "value": "string"}]
        return out

    lines = []
    run_ingest(
        conn,
        shop,
        lambda: FakeProvider(changed),
        session_id="orders-1",
        redo=True,
        yes=True,
        out=lines.append,
    )
    assert any("removed 1 fact" in line for line in lines)
    provides = q.list_facts(conn, kind="provides")
    assert len(provides) == 1 and provides[0].details[0]["value"] == "string"
    assert q.list_facts(conn, include_inactive=True, kind="provides") == provides


def test_forget_session_keeps_shared_facts_and_restores_what_it_replaced():
    from seamline.resolve.matcher import interface_id_for
    from seamline.resolve.supersede import Candidate, store_fact

    conn = connect(":memory:")
    iid = interface_id_for(conn, "order.created", "event")

    def put(session, ts, value, line=1):
        return store_fact(
            conn,
            Candidate(
                kind="provides",
                origin="session",
                service="orders",
                attributed_by="folder",
                interface_id=iid,
                claim="orders provides order.created",
                details=[{"name": "amount_cents", "value": value}],
                confidence=0.9,
                quote="q",
                session_id=session,
                line_no=line,
                timestamp=ts,
            ),
        ).fact_id

    shared = put("a", "2026-09-01T00:00:00Z", "integer")
    assert put("b", "2026-09-02T00:00:00Z", "integer", line=7) == shared  # duplicate
    newer = put("c", "2026-09-03T00:00:00Z", "string")  # supersedes the shared fact
    assert q.get_fact(conn, shared).status == "superseded"

    assert q.forget_session(conn, "c") == (1, 0)  # the only fact from c is deleted…
    assert q.get_fact(conn, shared).status == "active"  # …and what it replaced comes back
    assert conn.execute("SELECT 1 FROM facts WHERE id = ?", (newer,)).fetchone() is None

    assert q.forget_session(conn, "a") == (0, 1)  # shared fact keeps b's evidence
    assert [e["session_id"] for e in q.evidence_for(conn, shared)] == ["b"]


def test_ingest_records_spend_and_shows_it_against_the_cap(shop):
    conn = connect(":memory:")
    lines = []
    run_ingest(conn, shop, lambda: FakeProvider(model), yes=True, out=lines.append)
    assert "Spent today: $0.00 of the $5.00 daily cap" in lines
    assert conn.execute("SELECT COUNT(*) FROM spend").fetchone()[0] >= 1
    assert "This run cost $0.00." in lines


def test_budget_stop_ends_the_session_without_marking_it_read(shop):
    from seamline.extract.budget import BudgetExceeded

    conn = connect(":memory:")
    provider = FakeProvider([BudgetExceeded("daily cap reached")] * 5)
    run_ingest(conn, shop, lambda: provider, yes=True, out=lambda _: None)
    assert len(provider.prompts) == 1  # Stopped after the first refusal, per session
    assert q.all_session_states(conn) == {}
