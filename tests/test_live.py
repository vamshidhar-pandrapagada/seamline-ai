"""Phase 4 end to end with a fake model: hooks flag sessions, the worker ingests them, and
sessions get a brief and updates."""

import io
import json
import os
import time
import uuid

import pytest
from shop_project import ORDERS_SAYS, model

from seamline.brief import build_brief, build_update
from seamline.extract.providers import FakeProvider
from seamline.hooks import dispatch
from seamline.ledger import queries as q
from seamline.ledger.db import open_ledger
from seamline.ledger_views import run_ingest
from seamline.worker import run_worker
from seamline.worker_control import read_status


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "spawn_worker", lambda root: calls.append(root))
    monkeypatch.delenv("SEAMLINE_INTERNAL", raising=False)
    return calls


def transcript(home, session_id):
    return next(home.glob(f"projects/*/{session_id}.jsonl"))


def hook(event, shop, home, session_id, folder):
    out = io.StringIO()
    payload = {
        "session_id": session_id,
        "transcript_path": str(transcript(home, session_id)),
        "cwd": str(shop.root / folder),
        "hook_event_name": event,
    }
    assert dispatch.main([event], io.StringIO(json.dumps(payload)), out) == 0
    return out.getvalue()


def fake_clock(start):
    now = [start]
    return (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds))


def test_stop_flags_the_session_and_starts_a_worker(shop, isolated_claude_home, spawned):
    assert hook("Stop", shop, isolated_claude_home, "orders-1", "services/orders") == ""
    conn = open_ledger(shop.root)
    (row,) = q.work_queue(conn)
    assert (row["session_id"], row["service"], row["dirty"], row["urgent"]) == (
        "orders-1",
        "orders",
        1,
        0,
    )
    assert spawned == [shop.root]
    log = (shop.root / ".seamline/logs/hooks.log").read_text()
    assert "Stop services/orders" in log and " ok" in log


def test_prompt_in_another_session_makes_the_first_urgent(shop, isolated_claude_home, spawned):
    hook("Stop", shop, isolated_claude_home, "orders-1", "services/orders")
    hook("UserPromptSubmit", shop, isolated_claude_home, "root-1", ".")
    rows = {r["session_id"]: r for r in q.work_queue(open_ledger(shop.root))}
    assert rows["orders-1"]["urgent"] == 1  # Catch up on switch
    assert "root-1" not in rows  # A prompt doesn't flag its own session


def test_worker_waits_for_idle_then_ingests(shop, isolated_claude_home, spawned):
    hook("Stop", shop, isolated_claude_home, "orders-1", "services/orders")
    clock, sleep = fake_clock(time.time())
    status = run_worker(shop, lambda: FakeProvider(model), clock=clock, sleep=sleep)
    assert status == 0
    assert clock() - time.time() >= shop.worker.idle_minutes * 60 - 60  # It waited
    conn = open_ledger(shop.root)
    assert [f.kind for f in q.list_facts(conn)] == ["provides"]
    assert q.work_queue(conn) == []
    assert read_status(shop.root)["state"] == "idle"


def test_urgent_sessions_skip_the_wait(shop, isolated_claude_home, spawned):
    hook("SessionEnd", shop, isolated_claude_home, "orders-1", "services/orders")
    slept = []
    run_worker(shop, lambda: FakeProvider(model), sleep=slept.append)
    assert slept == []
    assert len(q.list_facts(open_ledger(shop.root))) == 1


def test_worker_stops_at_the_daily_cap_and_keeps_the_lines(shop, isolated_claude_home, spawned):
    hook("SessionEnd", shop, isolated_claude_home, "orders-1", "services/orders")
    conn = open_ledger(shop.root)
    from datetime import date

    q.record_spend(conn, date.today().isoformat(), "claude-opus-5", "record_facts", 0, 0, 4.999)
    conn.commit()
    provider = FakeProvider(model)
    assert run_worker(shop, lambda: provider) == 0
    assert provider.prompts == []  # Refused before calling the model
    status = read_status(shop.root)
    assert status["state"] == "budget" and "daily cap" in status["message"]
    assert q.work_queue(conn)[0]["session_id"] == "orders-1"  # Still flagged for tomorrow
    assert q.session_state(conn, "orders-1")["read_offset"] == 0

    # A second worker the same day exits at once
    assert run_worker(shop, lambda: pytest.fail("model called")) == 0


def test_budget_refusal_from_the_provider_stops_the_worker(shop, isolated_claude_home, spawned):
    hook("SessionEnd", shop, isolated_claude_home, "orders-1", "services/orders")
    shop.extract.model = "future-model"  # No price: a capped worker can't run it
    assert run_worker(shop, lambda: FakeProvider(model)) == 0
    assert read_status(shop.root)["state"] == "budget"
    assert "no price known" in read_status(shop.root)["message"]


def test_one_worker_at_a_time(shop, isolated_claude_home, spawned):
    from seamline.worker_control import acquire_lock, worker_running

    held = acquire_lock(shop.root)
    assert worker_running(shop.root)
    hook("SessionEnd", shop, isolated_claude_home, "orders-1", "services/orders")
    assert spawned == []  # A worker is running: the hook doesn't start another
    assert run_worker(shop, lambda: pytest.fail("second worker ran")) == 0
    held.close()
    assert not worker_running(shop.root)


def test_session_start_gets_a_brief_with_mismatches_first(shop, isolated_claude_home, spawned):
    run_ingest(open_ledger(shop.root), shop, lambda: FakeProvider(model), yes=True, out=lambda _: 0)
    text = hook("SessionStart", shop, isolated_claude_home, "orders-1", "services/orders")
    assert text.startswith("Seamline: shared memory")
    assert text.index("Open contract mismatches") < text.index("What other services say")
    assert "amount" in text
    assert len(text) <= shop.brief.max_tokens * 4


def test_update_tells_other_sessions_once(shop, isolated_claude_home, spawned):
    conn = open_ledger(shop.root)
    # Payments session starts on an empty ledger: nothing to brief
    assert hook("SessionStart", shop, isolated_claude_home, "root-1", ".") == ""
    # Meanwhile the orders session is ingested
    run_ingest(
        conn, shop, lambda: FakeProvider(model), session_id="orders-1", yes=True, out=lambda _: 0
    )
    text = hook("UserPromptSubmit", shop, isolated_claude_home, "root-1", ".")
    assert text.startswith("Seamline update")
    assert ORDERS_SAYS in text
    assert hook("UserPromptSubmit", shop, isolated_claude_home, "root-1", ".") == ""  # Once


def test_own_facts_are_not_repeated_back(shop, isolated_claude_home, spawned):
    conn = open_ledger(shop.root)
    hook("SessionStart", shop, isolated_claude_home, "orders-1", "services/orders")
    run_ingest(
        conn, shop, lambda: FakeProvider(model), session_id="orders-1", yes=True, out=lambda _: 0
    )
    assert hook("UserPromptSubmit", shop, isolated_claude_home, "orders-1", "services/orders") == ""


def test_brief_respects_its_token_cap(shop):
    conn = open_ledger(shop.root)
    from seamline.resolve.supersede import Candidate, store_fact

    with conn:
        for i in range(60):
            store_fact(
                conn,
                Candidate(
                    kind="decision",
                    origin="session",
                    service="orders",
                    attributed_by="name",
                    interface_id=None,
                    claim=f"orders stores setting {uuid.uuid4().hex} as {uuid.uuid4().hex}",
                    details=[],
                    confidence=0.9,
                    quote=f"quote {i}",
                    session_id=f"s{i}",
                    line_no=1,
                    timestamp=None,
                    files=[],
                    prompt_version="t",
                ),
            )
    text = build_brief(conn, shop, "orders")
    assert len(text) <= shop.brief.max_tokens * 4
    assert text.endswith("with quotes)") and "more;" in text
    assert build_update(conn, shop, "payments", "x", 0) == ""  # Orders-only decisions


def test_hook_never_fails(shop, isolated_claude_home, spawned, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(dispatch, "handle", boom)
    assert hook("Stop", shop, isolated_claude_home, "orders-1", "services/orders") == ""
    log = (shop.root / ".seamline/logs/hooks.log").read_text()
    assert "error" in log and "ledger exploded" in log


def test_hook_outside_a_project_or_paused_or_internal_does_nothing(
    shop, isolated_claude_home, spawned, tmp_path, monkeypatch
):
    out = io.StringIO()
    payload = json.dumps({"session_id": "x", "cwd": str(tmp_path / "elsewhere")})
    (tmp_path / "elsewhere").mkdir()
    assert dispatch.main(["Stop"], io.StringIO(payload), out) == 0
    (shop.root / ".seamline").mkdir(exist_ok=True)
    (shop.root / ".seamline/paused").touch()
    hook("Stop", shop, isolated_claude_home, "orders-1", "services/orders")
    os.remove(shop.root / ".seamline/paused")
    monkeypatch.setenv("SEAMLINE_INTERNAL", "1")
    hook("Stop", shop, isolated_claude_home, "orders-1", "services/orders")
    assert not (shop.root / ".seamline/ledger.db").exists() or not q.work_queue(
        open_ledger(shop.root)
    )
    assert spawned == []


def test_lines_written_during_ingest_stay_flagged(shop, isolated_claude_home, spawned):
    hook("SessionEnd", shop, isolated_claude_home, "orders-1", "services/orders")
    path = transcript(isolated_claude_home, "orders-1")

    def model_that_sees_new_lines(p):
        if len(provider.prompts) > 1:
            return model(p)
        with open(path, "a") as f:  # The user keeps chatting while the worker runs
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "late",
                        "sessionId": "orders-1",
                        "cwd": str(shop.root / "services/orders"),
                        "timestamp": "2026-09-01T10:05:00Z",
                        "message": {"role": "user", "content": "one more thing"},
                    }
                )
                + "\n"
            )
        return model(p)

    provider = FakeProvider(model_that_sees_new_lines)
    clock, sleep = fake_clock(time.time())
    run_worker(shop, lambda: provider, clock=clock, sleep=sleep)
    assert len(provider.prompts) == 2  # Came back for the late line once it was quiet


def test_worker_error_is_recorded(shop, isolated_claude_home, spawned):
    from seamline.extract.providers import ProviderAuthError

    hook("SessionEnd", shop, isolated_claude_home, "orders-1", "services/orders")
    run_worker(shop, lambda: FakeProvider([ProviderAuthError("no key")]))
    status = read_status(shop.root)
    assert status["state"] == "error" and "no key" in status["message"]
    assert q.work_queue(open_ledger(shop.root))  # Still flagged


def test_mismatch_resolved_and_reopened_unchanged_is_not_news(shop, isolated_claude_home, spawned):
    from seamline.resolve.drift import recompute

    conn = open_ledger(shop.root)
    run_ingest(conn, shop, lambda: FakeProvider(model), yes=True, out=lambda _: 0)
    hook("SessionStart", shop, isolated_claude_home, "root-1", ".")
    (m,) = q.open_mismatches(conn)
    # The provider fact goes stale (code changed), then a new one with the same problem lands
    with conn:
        q.mark_stale(conn, m["provides_fact"], "code")
        recompute(conn)
        assert q.open_mismatches(conn) == []
        q.restore(conn, m["provides_fact"])
        recompute(conn)
    assert len(q.open_mismatches(conn)) == 1
    text = hook("UserPromptSubmit", shop, isolated_claude_home, "root-1", ".")
    assert "Resolved" not in text and "New mismatch" not in text


def test_a_long_first_line_is_shortened_not_dropped(shop):
    from seamline.brief import _fit

    text = _fit("Header", [("", ["- " + "word " * 200, "- second"])], 100)
    assert text.splitlines()[1].endswith("…")
    assert len(text) <= 400
    assert text.endswith("with quotes)")
