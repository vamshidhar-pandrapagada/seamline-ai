from pathlib import Path

import pytest
from shop_project import make_shop


@pytest.fixture
def make_tree(tmp_path: Path):
    """Create files from relative paths; a trailing '/' makes an empty folder."""

    def make(*entries: str, root: Path | None = None) -> Path:
        base = root or tmp_path
        for entry in entries:
            p = base / entry
            if entry.endswith("/"):
                p.mkdir(parents=True, exist_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("")
        return base

    return make


@pytest.fixture(autouse=True)
def isolated_claude_home(tmp_path_factory, monkeypatch):
    """Never read the real ~/.claude in tests."""
    home = tmp_path_factory.mktemp("claude-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


@pytest.fixture
def shop(tmp_path, isolated_claude_home):
    """Project `shop`: services orders and payments, a session in each (see shop_project)."""
    return make_shop(tmp_path, isolated_claude_home)
