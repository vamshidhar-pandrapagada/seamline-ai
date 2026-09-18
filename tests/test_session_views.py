import pytest
from transcript_factory import prompt, reply, write_session

from seamline.config import ConfigError, parse_config
from seamline.session_views import resolve_project, show_session, show_sessions


@pytest.fixture
def shop(tmp_path, isolated_claude_home):
    root = tmp_path / "shop"
    (root / "orders").mkdir(parents=True)
    config = parse_config({"project": "shop", "services": {"orders": "orders"}}, root=root)
    history = [
        prompt("p1", "emit amount_cents"),
        reply("r1", "done"),
        {"type": "attachment", "uuid": "a1", "attachment": {"type": "date"}},
    ]
    write_session(
        isolated_claude_home,
        root / "orders",
        "parent-1",
        history,
        started="2026-09-01T10:00:00.000Z",
    )
    write_session(
        isolated_claude_home,
        root / "orders",
        "child-1",
        [*history, prompt("p2", "now add currency")],
        started="2026-09-02T10:00:00.000Z",
    )
    return config


def test_show_sessions(shop):
    lines = []
    show_sessions(shop, out=lines.append)
    rows = [line for line in lines if line.startswith("orders")]
    assert len(rows) == 2
    assert "2 session(s)" in lines[0]


def test_show_session_hides_skip_and_marks_inherited(shop):
    lines = []
    show_session(shop, "child", out=lines.append)
    text = "\n".join(lines)
    assert "service  orders" in text
    assert "3 inherited" in text
    assert "now add currency" in text
    assert "date" not in text.split("\n\n", 1)[1].split("(")[0]  # attachment hidden
    lines.clear()
    show_session(shop, "child", show_all=True, out=lines.append)
    inherited_rows = [line for line in lines if "^" in line[:8]]
    assert len(inherited_rows) == 3


def test_resolve_project(tmp_path, monkeypatch):
    (tmp_path / "seamline.toml").write_text('project = "p"\n')
    (tmp_path / "sub").mkdir()
    assert resolve_project(str(tmp_path / "sub")).project == "p"
    bare = tmp_path.parent / (tmp_path.name + "-bare")
    bare.mkdir()
    assert resolve_project(str(bare)).services == {}
    monkeypatch.chdir(bare)
    with pytest.raises(ConfigError, match="not inside"):
        resolve_project(None)
