import json

import pytest
from transcript_factory import prompt, write_session

from seamline.cli import main
from seamline.config import load_config
from seamline.paths import encode_project_dir
from seamline.project_init import InitError, run_init


@pytest.fixture
def shop(make_tree):
    return make_tree(
        "services/orders/pyproject.toml",
        "services/payments/pyproject.toml",
        "services/notifications/pyproject.toml",
        "proto/shop/orders.proto",
        "docker-compose.yml",
        ".git/",
    )


def quiet(*_):
    pass


def test_init_yes_writes_config(shop):
    result = run_init(shop, yes=True, out=quiet)
    cfg = load_config(shop / "seamline.toml")
    assert cfg.services == {
        "notifications": "services/notifications",
        "orders": "services/orders",
        "payments": "services/payments",
    }
    assert cfg.contracts == ["proto", "docker-compose.yml"]
    assert (shop / ".seamline/logs").is_dir()
    assert ".seamline/" in (shop / ".gitignore").read_text().splitlines()
    assert any("no Claude Code sessions" in w for w in result.warnings)
    assert any("cleanupPeriodDays" in w for w in result.warnings)


def test_init_refuses_existing_config(shop):
    run_init(shop, yes=True, out=quiet)
    with pytest.raises(InitError, match="already exists"):
        run_init(shop, yes=True, out=quiet)
    run_init(shop, yes=True, force=True, out=quiet)


def test_init_refuses_inside_other_project(shop):
    run_init(shop, yes=True, out=quiet)
    with pytest.raises(InitError, match="already inside"):
        run_init(shop / "services/orders", yes=True, out=quiet)


def test_interactive_rename_drop_and_contracts(shop):
    answers = iter(
        [
            "notify",  # rename notifications
            "Bad Name",  # rejected
            "-",  # drop orders
            "",  # keep payments
            "n",  # drop proto
            "",  # keep compose
        ]
    )
    run_init(shop, ask=lambda _: next(answers), out=quiet)
    cfg = load_config(shop / "seamline.toml")
    assert cfg.services == {"notify": "services/notifications", "payments": "services/payments"}
    assert cfg.contracts == ["docker-compose.yml"]


def test_gitignore_not_duplicated(shop):
    (shop / ".gitignore").write_text("node_modules\n.seamline\n")
    run_init(shop, yes=True, out=quiet)
    assert (shop / ".gitignore").read_text() == "node_modules\n.seamline\n"


def test_no_gitignore_outside_git(make_tree):
    root = make_tree("a/package.json")
    run_init(root, yes=True, out=quiet)
    assert not (root / ".gitignore").exists()


def test_counts_sessions_and_reads_cleanup(shop, isolated_claude_home):
    (isolated_claude_home / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 365}))
    write_session(isolated_claude_home, shop / "services/orders", "abc", [prompt("p", "hi")])
    lines: list[str] = []
    result = run_init(shop, yes=True, out=lines.append)
    assert result.warnings == []
    assert any(line.split() == ["orders", "1"] for line in lines)


def test_low_cleanup_period_warns(shop, isolated_claude_home):
    (isolated_claude_home / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 30}))
    result = run_init(shop, yes=True, out=quiet)
    assert any("after 30 days" in w for w in result.warnings)


def test_encode_project_dir(tmp_path):
    folder = tmp_path / "my.project_ai"
    folder.mkdir()
    encoded = encode_project_dir(folder)
    assert encoded.endswith("-my-project-ai")
    assert all(c.isalnum() or c == "-" for c in encoded)


def test_cli_init_and_placeholders(shop, capsys):
    assert main(["init", str(shop), "--yes"]) == 0
    assert main(["init", str(shop), "--yes"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert (shop / ".claude" / "settings.local.json").exists()  # init installs hooks
    assert (shop / ".mcp.json").exists()  # and registers the MCP server
