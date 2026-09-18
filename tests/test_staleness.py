"""Session facts go stale when their service's code changes and stops using a field."""

import os
import time

import pytest

from seamline.config import parse_config
from seamline.ledger import queries as q
from seamline.ledger.db import connect
from seamline.resolve.drift import recompute
from seamline.resolve.matcher import interface_id_for
from seamline.resolve.staleness import field_used, refresh
from seamline.resolve.supersede import Candidate, store_fact


@pytest.mark.parametrize(
    "code",
    [
        "amount = Decimal(str(event['amount']))",  # Python dict key
        'payload = {"amount": 12}',  # JSON-ish literal
        "total = order.amount",  # attribute
        "@dataclass\nclass Order:\n    amount: Decimal",  # dataclass field
        "interface Order { amount?: number }",  # TypeScript
        "pub struct Order { pub amount: i64 }",  # Rust
        "message Order { int64 amount = 2; }",  # proto
        'Amount int64 `json:"amount"`',  # Go json tag
        "amount:\n  type: number",  # YAML / OpenAPI
    ],
)
def test_field_used(code):
    assert field_used("amount", code)


@pytest.mark.parametrize(
    "code",
    [
        "cents = event['amount_cents']",  # a different field containing the word
        "amount = 5\nprint(amount)",  # a local variable, not a field
        "# the amount is in dollars",  # prose
        "total_amount = order.total_amount",
    ],
)
def test_field_not_used(code):
    assert not field_used("amount", code)


def test_spellings_across_languages():
    assert field_used("amount_cents", "const x = order.amountCents")
    assert field_used("amountCents", "cents = event['amount_cents']")
    assert field_used("order_id", 'type Event struct { OrderID string `json:"orderID"` }')
    assert field_used("api_url", "cfg.apiURL")


@pytest.fixture
def project(tmp_path):
    for s in ("orders", "payments"):
        (tmp_path / "services" / s).mkdir(parents=True)
    charge = tmp_path / "services" / "payments" / "charge.py"
    charge.write_text("amount = Decimal(str(event['amount']))\n")
    old = time.time() - 3600
    os.utime(charge, (old, old))  # code written an hour ago
    config = parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders", "payments": "services/payments"},
        },
        root=tmp_path,
    )
    return config, charge


def put(conn, kind, service, details, ts):
    return store_fact(
        conn,
        Candidate(
            kind=kind,
            origin="session",
            service=service,
            attributed_by="folder",
            interface_id=interface_id_for(conn, "order.created", "event"),
            claim=f"{service} {kind}",
            details=[{"name": k, "value": v} for k, v in details.items()],
            confidence=0.9,
            quote="q",
            session_id=service,
            line_no=1,
            timestamp=ts,
        ),
    ).fact_id


def now_iso(offset_s=0):
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()


def test_silent_code_fix_marks_stale_and_resolves_drift_then_restores(project):
    config, charge = project
    conn = connect(":memory:")
    put(conn, "provides", "orders", {"amount_cents": "integer"}, now_iso(-7200))
    wrong = put(conn, "assumes", "payments", {"field_amount": "decimal"}, now_iso(-1800))
    recompute(conn)
    assert len(q.open_mismatches(conn)) == 1

    # Code unchanged since the statement: still backed (and "field_" prefix is handled)
    assert refresh(conn, config).marked_stale == []

    charge.write_text("amount = Decimal(event['amount_cents']) / 100\n")  # fixed, silently
    result = refresh(conn, config)
    assert [fid for fid, _ in result.marked_stale] == [wrong]
    assert "no longer uses `field_amount`" in result.marked_stale[0][1]
    recompute(conn)
    assert q.open_mismatches(conn) == []

    charge.write_text("amount = Decimal(str(event['amount']))\n")  # reverted
    assert refresh(conn, config).restored == [wrong]
    recompute(conn)
    assert len(q.open_mismatches(conn)) == 1
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM changes")]
    assert "fact_stale" in kinds and "fact_restored" in kinds


def test_contract_not_written_yet_is_not_stale(project):
    config, _ = project
    conn = connect(":memory:")
    # orders has no code at all, and payments' code predates this statement
    put(conn, "provides", "orders", {"amount_cents": "integer"}, now_iso())
    put(conn, "assumes", "payments", {"currency": "string"}, now_iso())
    result = refresh(conn, config)
    assert result.marked_stale == []
    assert result.skipped_unchanged == 1  # payments: missing, but code older than the fact


def test_only_session_interface_facts_with_typed_fields_are_checked(project):
    config, charge = project
    conn = connect(":memory:")
    charge.write_text("print('rewritten')\n")
    put(conn, "assumes", "payments", {"unit": "dollars"}, now_iso(-600))  # no typed field
    store_fact(
        conn,
        Candidate(
            kind="decision",
            origin="session",
            service="payments",
            attributed_by="folder",
            interface_id=None,
            claim="Use Stripe.",
            details=[],
            confidence=0.9,
            quote="q",
            session_id="s",
            line_no=1,
            timestamp=now_iso(-600),
        ),
    )
    assert refresh(conn, config).checked == 0
