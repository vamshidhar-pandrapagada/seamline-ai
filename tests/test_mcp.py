"""Phase 5: the MCP tools over a ledger filled by the fake model, the catch-up on demand,
and the `.mcp.json` entry."""

import asyncio
import json

import pytest
from shop_project import ORDERS_SAYS, model

from seamline.extract.providers import FakeProvider
from seamline.ledger import queries as q
from seamline.ledger.db import open_ledger
from seamline.ledger_views import run_ingest
from seamline.mcp_server import freshness, registration, tools
from seamline.mcp_server.server import build_server
from seamline.resolve.supersede import Candidate, store_fact


@pytest.fixture
def filled(shop):
    conn = open_ledger(shop.root)
    run_ingest(conn, shop, lambda: FakeProvider(model), yes=True, out=lambda _: 0)
    with conn:
        for kind, claim in (
            ("dead_end", "retrying the charge on timeout double-charged customers"),
            ("decision", "payments converts amount_cents to dollars at the edge"),
        ):
            store_fact(
                conn,
                Candidate(
                    kind=kind,
                    origin="session",
                    service="payments",
                    attributed_by="name",
                    interface_id=None,
                    claim=claim,
                    details=[],
                    confidence=0.9,
                    quote=claim,
                    session_id="root-1",
                    line_no=7,
                    timestamp="2026-09-02T10:05:00Z",
                ),
            )
    return shop, conn


def test_integration_context_shows_both_sides_and_mismatches_first(filled):
    shop, conn = filled
    text = tools.integration_context(conn, shop, ["orders", "payments"])
    assert text.index("## Open mismatches") < text.index("## order.created")
    assert "[orders provides]" in text and "[payments assumes]" in text
    assert "(session orders-1 L" in text  # Every fact cites its source
    assert "Unknown service" in tools.integration_context(conn, shop, ["billing"])


def test_check_contract_matches_any_spelling_and_quotes(filled):
    shop, conn = filled
    for spelling in ("order.created", "OrderCreated", "ORDER_CREATED"):
        text = tools.contract(conn, shop, spelling)
        assert text.startswith("# order.created"), spelling
    assert f'"{ORDERS_SAYS}"' in text
    assert "No interface named" in tools.contract(conn, shop, "invoice.paid")
    # A qualified name with another package still finds the message by its last part
    assert tools.contract(conn, shop, "shop.v2.order.created").startswith("# order.created")


def test_dead_ends_and_history_search_by_topic(filled):
    shop, conn = filled
    assert "double-charged" in tools.dead_ends(conn, "retry timeout")
    assert tools.dead_ends(conn, "kafka") == "No dead ends recorded about 'kafka'."
    assert "converts amount_cents" in tools.history(conn, "dollars")
    assert "converts amount_cents" in tools.history(conn, "")  # Empty query: everything


def test_remember_stores_user_facts_once(filled):
    shop, conn = filled
    said = "Refunds always go through the payments service, never orders."
    first = tools.remember(conn, shop, said, service="payments", session_id="s1")
    assert first.startswith("Remembered as a decision for payments")
    again = tools.remember(conn, shop, said, service="payments", session_id="s2")
    assert again.startswith("Already known")
    (f,) = [f for f in q.list_facts(conn) if f.origin == "user"]
    assert f.claim == said
    assert "Not stored" in tools.remember(conn, shop, "short")
    assert "unknown service" in tools.remember(conn, shop, said, service="billing")


def test_answers_are_capped(filled, monkeypatch):
    shop, conn = filled
    monkeypatch.setattr(tools, "MAX_CHARS", 200)
    text = tools.contract(conn, shop, "order.created")
    assert len(text) < 300 and "more lines cut" in text


def test_catch_up_waits_for_the_worker_and_skips_the_calling_session(filled):
    shop, conn = filled
    with conn:
        for sid, service in (("orders-1", "orders"), ("root-1", "integration")):
            q.touch_session(conn, sid, service=service, transcript_path="x", dirty=True)
    started = []

    def fake_worker(root):  # Pretend the worker ingests everything at once
        started.append(root)
        with conn:
            q.clear_flags(conn, "orders-1")

    left = freshness.catch_up(
        conn, shop, {"orders"}, "root-1", start_worker=fake_worker, sleep=lambda _: None
    )
    assert left == [] and started == [shop.root]
    assert q.session_state(conn, "orders-1")["urgent"] == 0
    assert q.session_state(conn, "root-1")["urgent"] == 0  # The caller's own lines: not urged


def test_catch_up_times_out_and_says_so(filled):
    shop, conn = filled
    with conn:
        q.touch_session(conn, "orders-1", service="orders", transcript_path="x", dirty=True)
    now = [0.0]
    left = freshness.catch_up(
        conn,
        shop,
        None,
        None,
        timeout=3,
        clock=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        start_worker=lambda root: None,
    )
    assert [r["session_id"] for r in left] == ["orders-1"]
    assert "Still reading orders orders-1" in freshness.describe(conn, None, left)


def test_server_lists_five_tools_and_answers_over_mcp(filled):
    shop, _ = filled

    async def go():
        server = build_server(shop, own_session="root-1")
        names = sorted(t.name for t in await server.list_tools())
        result = await server.call_tool("check_contract", {"interface": "order.created"})
        return names, result

    names, result = asyncio.run(go())
    assert names == [
        "check_contract",
        "find_dead_ends",
        "get_integration_context",
        "remember",
        "search_history",
    ]
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "# order.created" in text and "Freshness:" in text


def test_mcp_json_entry_is_added_merged_and_removed(tmp_path):
    from seamline.config import parse_config

    config = parse_config({"project": "p"}, root=tmp_path)
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    registration.install(config, python="/py")
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["seamline"]["args"] == [
        "-m",
        "seamline",
        "mcp",
        "--root",
        str(tmp_path),
    ]
    assert registration.installed(config)
    assert registration.install(config, python="/py") == ["MCP server already in .mcp.json"]
    assert registration.uninstall(config)
    assert json.loads(path.read_text()) == {"mcpServers": {"other": {"command": "x"}}}
    path.unlink()
    registration.install(config, python="/py")
    registration.uninstall(config)
    assert not path.exists()  # A file only Seamline used is deleted


def test_freshness_counts_project_root_sessions_for_any_service(filled):
    shop, conn = filled
    with conn:
        conn.execute("UPDATE sessions SET last_ingested = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
    text = freshness.describe(conn, {"orders"}, [])
    assert "integration session root-1 read just now" in text
