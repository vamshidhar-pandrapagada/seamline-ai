"""Seamline's entry in the project's `.mcp.json`.

Unlike hooks, Claude Code looks for `.mcp.json` in the session's folder and its parents, so
one file at the project root serves sessions started in any service folder (checked with
`claude mcp list`, Claude Code 2.1.276). Claude Code asks you to approve a project's MCP
server the first time; Seamline doesn't approve itself. Other servers in the file are left
as they are.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from seamline.config import Config

SERVER_NAME = "seamline"
FILENAME = ".mcp.json"


class McpConfigError(Exception):
    """`.mcp.json` exists but isn't JSON we can safely edit."""


def path_for(config: Config) -> Path:
    return config.root / FILENAME


def entry(config: Config, python: str | None = None) -> dict:
    return {
        "command": python or sys.executable,
        "args": ["-m", "seamline", "mcp", "--root", str(config.root)],
    }


def install(config: Config, python: str | None = None) -> list[str]:
    """Add or refresh the entry. Returns notes to show the user."""
    path = path_for(config)
    existed = path.exists()
    data = _load(path)
    servers = data.setdefault("mcpServers", {})
    wanted = entry(config, python)
    notes = []
    if servers.get(SERVER_NAME) == wanted:
        return [f"MCP server already in {FILENAME}"]
    servers[SERVER_NAME] = wanted
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    notes.append(f"Added the seamline MCP server to {FILENAME}")
    if not existed:
        note = _gitignore(config)
        if note:
            notes.append(note)
    elif _tracked(config.root, path):
        notes.append(
            f"note: {FILENAME} is committed and now holds this machine's paths for seamline; "
            "keep that change out of commits"
        )
    return notes


def uninstall(config: Config) -> bool:
    path = path_for(config)
    if not path.exists():
        return False
    data = _load(path)
    servers = data.get("mcpServers", {})
    if SERVER_NAME not in servers:
        return False
    del servers[SERVER_NAME]
    if not servers:
        data.pop("mcpServers")
    if data:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


def installed(config: Config) -> bool:
    try:
        return SERVER_NAME in _load(path_for(config)).get("mcpServers", {})
    except McpConfigError:
        return False


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as e:
        raise McpConfigError(f"{path}: not valid JSON ({e}); fix or remove it first") from e
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise McpConfigError(f"{path}: unexpected layout; fix or remove it first")
    return data


def _gitignore(config: Config) -> str | None:
    """A .mcp.json Seamline created holds this machine's paths: keep it out of git."""
    root = config.root
    if _git(root, "rev-parse", "--is-inside-work-tree") != 0:
        return None
    if _git(root, "check-ignore", "-q", "--no-index", str(path_for(config))) == 0:
        return None
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(f"{existing}{sep}/{FILENAME}\n", encoding="utf-8")
    return f"Added /{FILENAME} to .gitignore"


def _tracked(root: Path, path: Path) -> bool:
    return _git(root, "ls-files", "--error-unmatch", str(path)) == 0


def _git(root: Path, *args: str) -> int:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=5
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1
