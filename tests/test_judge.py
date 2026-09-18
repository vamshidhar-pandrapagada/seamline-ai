"""When is a newer fact a replacement? Rules for clear cases, the judge for ambiguous ones.

Each case is one row of the table in the design discussion: old fact, newer fact, what the
fallback rules do without a judge, and the right answer (which the judge supplies).
"""

import pytest

from seamline.extract.providers import FakeProvider, ProviderError
from seamline.ledger import queries as q
from seamline.ledger.db import connect
from seamline.resolve.judge import JUDGE_TOOL, LLMJudge, Verdict
from seamline.resolve.matcher import interface_id_for
from seamline.resolve.supersede import Candidate, store_fact


class StubJudge:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def replaces(self, newer, older):
        self.asked.append((newer, older))
        return None if self.answer is None else Verdict(self.answer, "stub")


def fields(**kw):
    return [{"name": k, "value": v} for k, v in kw.items()]


def store(conn, details, ts, judge=None, kind="assumes", claim=None):
    return store_fact(
        conn,
        Candidate(
            kind=kind,
            origin="session",
            service="payments",
            attributed_by="folder",
            interface_id=interface_id_for(conn, "order.created", "event")
            if kind != "decision"
            else None,
            claim=claim or f"payments assumes {details}",
            details=details,
            confidence=0.9,
            quote="q",
            session_id="s",
            line_no=1,
            timestamp=ts,
        ),
        judge,
    )


# (name, old fields, new fields, fallback replaces?, correct answer)
CASES = [
    ("rename sharing a word", fields(amount="decimal"), fields(amount_cents="integer"), True, True),
    (
        "same field, other spelling",
        fields(userId="string"),
        fields(user_id="uuid string"),
        True,
        True,
    ),
    (
        "full restatement, retyped",
        fields(amount_cents="integer"),
        fields(amount_cents="string"),
        True,
        True,
    ),
    (
        "rename sharing no word",
        fields(amount="decimal"),
        fields(total_cents="integer"),
        False,
        True,
    ),
    (
        "different fields sharing 'at'",
        fields(created_at="timestamp"),
        fields(updated_at="timestamp"),
        False,
        False,
    ),
    (
        "different fields sharing 'order'",
        fields(order_id="string"),
        fields(order_status="string"),
        True,
        False,
    ),
    ("newer one has no details", fields(amount_cents="integer"), [], False, False),
]


@pytest.mark.parametrize("name, old, new, fallback, correct", CASES, ids=[c[0] for c in CASES])
def test_fallback_rules_without_a_judge(name, old, new, fallback, correct):
    conn = connect(":memory:")
    first = store(conn, old, "2026-09-01T00:00:00Z")
    second = store(conn, new, "2026-09-02T00:00:00Z")
    assert (first.fact_id in second.replaced) is fallback
    # Where fallback != correct is a known limitation of word rules; the judge fixes it:


@pytest.mark.parametrize("name, old, new, fallback, correct", CASES, ids=[c[0] for c in CASES])
def test_judge_gives_the_right_answer_where_asked(name, old, new, fallback, correct):
    conn = connect(":memory:")
    judge = StubJudge(correct)
    first = store(conn, old, "2026-09-01T00:00:00Z")
    second = store(conn, new, "2026-09-02T00:00:00Z", judge)
    assert (first.fact_id in second.replaced) is correct
    # Rules settle full restatements without asking; everything else goes to the judge
    clear = name in ("same field, other spelling", "full restatement, retyped")
    assert (judge.asked == []) is clear
    assert second.judged == (0 if clear else 1)


def test_judge_failure_falls_back_to_rules():
    conn = connect(":memory:")
    first = store(conn, fields(amount="decimal"), "2026-09-01T00:00:00Z")
    second = store(conn, fields(amount_cents="integer"), "2026-09-02T00:00:00Z", StubJudge(None))
    assert second.replaced == [first.fact_id] and second.judged == 0


def test_decisions_unrelated_rules_ambiguous_judged():
    conn = connect(":memory:")
    judge = StubJudge(True)
    a = store(
        conn,
        [],
        "2026-09-01T00:00:00Z",
        judge,
        kind="decision",
        claim="Use Redis streams between orders and payments.",
    )
    b = store(
        conn,
        [],
        "2026-09-02T00:00:00Z",
        judge,
        kind="decision",
        claim="Retry failed card charges three times with backoff.",
    )
    assert judge.asked == [] and b.replaced == []  # clearly unrelated: no call
    c = store(
        conn,
        [],
        "2026-09-03T00:00:00Z",
        judge,
        kind="decision",
        claim="Use Kafka instead of Redis streams between orders and payments.",
    )
    assert len(judge.asked) == 1 and c.replaced == [a.fact_id]
    assert q.get_fact(conn, b.fact_id).status == "active"


def test_llm_judge_prompt_and_parsing():
    seen = {}

    def model(prompt):
        seen["prompt"] = prompt
        return {"replaces": True, "reason": "amount was renamed"}

    judge = LLMJudge(FakeProvider(model))
    v = judge.replaces({"details": {"amount_cents": "integer"}}, {"details": {"amount": "decimal"}})
    assert v == Verdict(True, "amount was renamed")
    assert seen["prompt"].index("OLDER") < seen["prompt"].index("NEWER")
    assert judge.calls == 1
    assert JUDGE_TOOL.name == "record_verdict"


def test_llm_judge_returns_none_on_errors_or_bad_output():
    assert LLMJudge(FakeProvider([ProviderError("down")])).replaces({}, {}) is None
    assert LLMJudge(FakeProvider([{"verdict": "yes"}])).replaces({}, {}) is None
