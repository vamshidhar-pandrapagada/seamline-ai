"""Seamline's entries in the project's `.claude/settings.local.json` files.

Claude Code applies a folder's project settings only to sessions started in exactly that
folder (docs/hook-behavior.md), so the hooks go into the project root and every service
folder. Nothing is written outside the project. Seamline's entries are recognized by their
command (`-m seamline.hooks`); everything else in the file is left as it was.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

from seamline.config import Config

EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "SessionEnd")
MARKER = "-m seamline.hooks"
TIMEOUT_SECONDS = 10  # Claude Code's limit per call; the hook itself aims for < 200 ms
GITIGNORE_ENTRY = "**/.claude/settings.local.json"


class SettingsError(Exception):
    """A settings file exists but isn't JSON we can safely edit."""


def settings_path(folder: Path) -> Path:
    return folder / ".claude" / "settings.local.json"


def hook_folders(config: Config) -> list[Path]:
    """Where sessions get hooks: the project root and each service folder."""
    return [config.root, *config.service_dirs().values()]


def command_for(event: str, python: str | None = None) -> str:
    """The hook command: this Python (the one Seamline is installed in), so hooks don't
    depend on PATH, which the desktop app doesn't share with your shell."""
    return f"{shlex.quote(python or sys.executable)} {MARKER} {event}"


def install(folder: Path, python: str | None = None) -> bool:
    """Add (or refresh) Seamline's hooks in a folder. True if the file changed."""
    path = settings_path(folder)
    data = _load(path)
    before = json.dumps(data, sort_keys=True)
    hooks = _strip(data)
    for event in EVENTS:
        hooks.setdefault(event, []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command_for(event, python),
                        "timeout": TIMEOUT_SECONDS,
                    }
                ]
            }
        )
    data["hooks"] = hooks
    if json.dumps(data, sort_keys=True) == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def uninstall(folder: Path) -> bool:
    """Remove Seamline's hooks from a folder. Deletes the file (and an empty .claude/) if
    nothing else is left in it. True if anything was removed."""
    path = settings_path(folder)
    if not path.exists():
        return False
    data = _load(path)
    before = json.dumps(data, sort_keys=True)
    hooks = _strip(data)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    if json.dumps(data, sort_keys=True) == before:
        return False
    if data:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        path.unlink()
        with contextlib.suppress(OSError):
            path.parent.rmdir()  # Only if empty
    return True


def installed(folder: Path) -> list[str]:
    """Events Seamline has a hook for in this folder."""
    path = settings_path(folder)
    if not path.exists():
        return []
    try:
        data = _load(path)
    except SettingsError:
        return []
    return [
        event
        for event, groups in data.get("hooks", {}).items()
        if any(_ours(h) for g in groups for h in g.get("hooks", []))
    ]


def ensure_gitignored(config: Config) -> str | None:
    """Keep the settings files out of git: they hold this machine's Python path."""
    root = config.root
    if not _in_git_repo(root):
        return None
    unignored = [
        settings_path(folder).relative_to(root).as_posix()
        for folder in hook_folders(config)
        if not _git_ignored(root, settings_path(folder))
    ]
    if not unignored:
        return None
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if GITIGNORE_ENTRY in {line.strip() for line in existing.splitlines()}:
        return None
    sep = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(f"{existing}{sep}{GITIGNORE_ENTRY}\n", encoding="utf-8")
    return f"Added {GITIGNORE_ENTRY} to .gitignore"


def _in_git_repo(root: Path) -> bool:
    """True inside any git work tree, including a project nested in a bigger repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _git_ignored(root: Path, path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--no-index", str(path)],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as e:
        raise SettingsError(f"{path}: not valid JSON ({e}); fix or remove it first") from e
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise SettingsError(f"{path}: unexpected layout; fix or remove it first")
    return data


def _ours(hook: dict) -> bool:
    return isinstance(hook, dict) and MARKER in str(hook.get("command", ""))


def _strip(data: dict) -> dict:
    """The hooks table without Seamline's entries (empty groups and events dropped)."""
    kept: dict = {}
    for event, groups in data.get("hooks", {}).items():
        new_groups = []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            inner = [h for h in group.get("hooks", []) if not _ours(h)]
            if inner:
                new_groups.append({**group, "hooks": inner})
            elif not group.get("hooks"):
                new_groups.append(group)
        if new_groups:
            kept[event] = new_groups
    return kept
