import pytest
from transcript_factory import prompt, reply, write_session

from seamline.config import parse_config
from seamline.transcripts.discover import discover, find_session, inherited_uuids
from seamline.transcripts.sources.claude_code import ClaudeCodeSource


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "shop"
    (root / "services/orders/src").mkdir(parents=True)
    (root / "services/payments").mkdir(parents=True)
    return parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders", "payments": "services/payments"},
        },
        root=root,
    )


def test_maps_sessions_to_services(shop, isolated_claude_home):
    home = isolated_claude_home
    write_session(home, shop.root, "root-1", [prompt("a", "hi")])
    write_session(home, shop.root / "services/orders", "orders-1", [prompt("b", "hi")])
    write_session(home, shop.root / "services/orders/src", "orders-2", [prompt("c", "hi")])
    write_session(home, shop.root / "services/payments", "pay-1", [prompt("d", "hi")])
    write_session(home, shop.root.parent / "elsewhere", "other-1", [prompt("e", "hi")])
    found = {s.info.session_id: s.service for s in discover(shop)}
    assert found == {
        "root-1": "integration",
        "orders-1": "orders",
        "orders-2": "orders",
        "pay-1": "payments",
    }


def test_lossy_encoding_resolved_by_recorded_cwd(tmp_path, isolated_claude_home):
    # shop_ai and shop-ai encode to the same folder name; the cwd inside decides.
    real = tmp_path / "shop_ai"
    twin = tmp_path / "shop-ai"
    real.mkdir()
    twin.mkdir()
    config = parse_config({"project": "shop"}, root=real)
    folder = "-" + str(real).strip("/").replace("/", "-").replace("_", "-")
    write_session(isolated_claude_home, real, "mine", [prompt("a", "x")], folder=folder)
    write_session(isolated_claude_home, twin, "theirs", [prompt("b", "x")], folder=folder)
    assert [s.info.session_id for s in discover(config)] == ["mine"]


def test_prefix_sibling_folder_excluded(tmp_path, isolated_claude_home):
    root = tmp_path / "shop"
    root.mkdir()
    config = parse_config({"project": "shop"}, root=root)
    write_session(isolated_claude_home, tmp_path / "shop-old", "old", [prompt("a", "x")])
    assert discover(config) == []


def test_no_projects_dir(shop):
    assert discover(shop) == []


def test_session_info(shop, isolated_claude_home):
    path = write_session(
        isolated_claude_home,
        shop.root,
        "s1",
        [prompt("a", "hi")],
        started="2026-09-02T08:00:00.000Z",
    )
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text("")
    (info,) = [s.info for s in discover(shop)]
    assert info.start_cwd == shop.root
    assert info.started == "2026-09-02T08:00:00.000Z"
    assert info.subagents == 1
    assert info.size == path.stat().st_size


def test_inherited_uuids_follow_creation_order(shop, isolated_claude_home):
    home = isolated_claude_home
    history = [prompt("p1", "first"), reply("r1", "ok")]
    write_session(home, shop.root, "parent", history, started="2026-09-01T10:00:00.000Z")
    # A resumed session copies the parent's records (same uuids and timestamps), then adds its own.
    write_session(
        home,
        shop.root,
        "child",
        [*history, prompt("p2", "more")],
        started="2026-09-05T10:00:00.000Z",
    )
    sessions = [s.info for s in discover(shop)]
    parent = find_session(sessions, "parent")
    child = find_session(sessions, "child")
    source = ClaudeCodeSource()
    assert inherited_uuids(child, sessions, source) == {"p1", "r1"}
    assert inherited_uuids(parent, sessions, source) == set()


def test_find_session(shop, isolated_claude_home):
    write_session(isolated_claude_home, shop.root, "abc-1", [prompt("a", "x")])
    write_session(isolated_claude_home, shop.root, "abd-2", [prompt("b", "x")])
    sessions = [s.info for s in discover(shop)]
    assert find_session(sessions, "abc").session_id == "abc-1"
    with pytest.raises(LookupError, match="several"):
        find_session(sessions, "ab")
    with pytest.raises(LookupError, match="no session"):
        find_session(sessions, "zzz")
