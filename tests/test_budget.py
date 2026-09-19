from datetime import date

import pytest

from seamline.extract.budget import BudgetExceeded, MeteredProvider, estimate_call_usd
from seamline.extract.providers import RECORD_FACTS, Completion, FakeProvider
from seamline.ledger import queries as q
from seamline.ledger.db import connect
from seamline.resolve.judge import JUDGE_TOOL

DAY = date(2026, 9, 18)


class Priced(FakeProvider):
    """A fake that reports a real model and fixed usage: 100k in + 10k out on Opus = $0.75."""

    def complete(self, system, prompt, schema, tool=RECORD_FACTS):
        super().complete(system, prompt, schema, tool)
        return Completion({"facts": []}, 100_000, 10_000, "claude-opus-5")


def metered(conn, cap=None, inner=None, model="claude-opus-5"):
    return MeteredProvider(inner or Priced([]), conn, model, cap_usd=cap, today=lambda: DAY)


def test_records_actual_usage_priced_by_the_answering_model():
    conn = connect(":memory:")
    provider = metered(conn)
    provider.complete("sys", "prompt", {})
    provider.complete("sys", "prompt", {}, JUDGE_TOOL)
    rows = conn.execute("SELECT day, model, purpose, usd FROM spend ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-09-18", "claude-opus-5", "record_facts", 0.75),
        ("2026-09-18", "claude-opus-5", "record_verdict", 0.75),
    ]
    assert q.spent_on(conn, "2026-09-18") == 1.5
    assert provider.spent_usd == 1.5


def test_cap_refuses_the_call_that_would_pass_it():
    conn = connect(":memory:")
    inner = Priced([])
    provider = metered(conn, cap=0.78, inner=inner)
    provider.complete("sys", "prompt", {})  # Estimated ~$0.04, actually cost $0.75
    with pytest.raises(BudgetExceeded, match=r"\$0.75 spent today.*cap \$0.78"):
        provider.complete("sys", "prompt", {})  # 0.75 + ~0.04 estimate would pass 0.78
    assert len(inner.prompts) == 1


def test_cap_counts_only_today():
    conn = connect(":memory:")
    q.record_spend(conn, "2026-09-17", "claude-opus-5", "record_facts", 0, 0, 4.99)
    metered(conn, cap=5.0).complete("sys", "prompt", {})  # Yesterday's spend doesn't count


def test_capped_calls_need_a_known_price_uncapped_ones_are_recorded_anyway():
    conn = connect(":memory:")
    with pytest.raises(BudgetExceeded, match="no price known"):
        metered(conn, cap=5.0, inner=FakeProvider([]), model="future-model").complete("s", "p", {})
    metered(conn, inner=FakeProvider([]), model="future-model").complete("s", "p", {})
    (row,) = conn.execute("SELECT model, usd FROM spend").fetchall()
    assert tuple(row) == ("fake", None)


def test_recording_inside_a_transaction_leaves_the_commit_to_the_caller():
    conn = connect(":memory:")
    conn.execute("INSERT INTO services (name, path) VALUES ('a', 'a')")
    assert conn.in_transaction
    metered(conn).complete("s", "p", {})
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == 0


def test_estimate_uses_prompt_size_and_expected_output():
    small = estimate_call_usd("claude-opus-5", "s" * 400, "p" * 4_000, JUDGE_TOOL)
    large = estimate_call_usd("claude-opus-5", "s" * 400, "p" * 4_000, RECORD_FACTS)
    assert small < large
    assert estimate_call_usd("future-model", "", "", RECORD_FACTS) is None
