"""Where things live: the project's .seamline/ folder and Claude Code's own files."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DATA_DIRNAME = ".seamline"


# --- .seamline/ inside a project ---------------------------------------------------------


def data_dir(root: Path) -> Path:
    return Path(root) / DATA_DIRNAME


def logs_dir(root: Path) -> Path:
    return data_dir(root) / "logs"


def ledger_path(root: Path) -> Path:
    return data_dir(root) / "ledger.db"


def worker_lock_path(root: Path) -> Path:
    return data_dir(root) / "worker.lock"


def worker_status_path(root: Path) -> Path:
    return data_dir(root) / "worker.json"


def paused_marker(root: Path) -> Path:
    return data_dir(root) / "paused"


def hooks_log_path(root: Path) -> Path:
    return logs_dir(root) / "hooks.log"


def worker_log_path(root: Path) -> Path:
    return logs_dir(root) / "worker.log"


# --- Claude Code ---------------------------------------------------------------------------


def claude_home() -> Path:
    """~/.claude, or $CLAUDE_CONFIG_DIR when set."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def claude_settings_path() -> Path:
    return claude_home() / "settings.json"


def encode_path(folder: Path | str) -> str:
    """Claude Code's folder name for a session's working directory.

    Every character that isn't a letter or digit becomes '-':
    /Users/me/code/shop_ai -> -Users-me-code-shop-ai. The mapping is lossy (shop_ai and
    shop-ai collide), so discovery trusts the `cwd` recorded inside each transcript.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(folder))


def encode_project_dir(folder: Path | str) -> str:
    """encode_path() of the folder with symlinks resolved."""
    return encode_path(Path(folder).resolve())


def read_cleanup_period_days() -> int | None:
    """`cleanupPeriodDays` from the user's Claude Code settings, or None if unset/unreadable."""
    try:
        settings = json.loads(claude_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = settings.get("cleanupPeriodDays") if isinstance(settings, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None
