"""`seamline install` / `seamline uninstall-global`: one user-level setup, so every session
started anywhere inside a Seamline project is tracked, including folders added later.

- Hooks go into your Claude Code user settings (`~/.claude/settings.json`). Each one is a
  small shell guard: it walks up from the session's folder looking for `seamline.toml` and
  exits at once (no Python, nothing read or recorded) when there is none.
- The MCP server is registered at user scope (`claude mcp add --scope user`). Outside a
  Seamline project it offers no tools. Its command is this installation's Python, so a
  cloned repo can't substitute its own "seamline" server.

Once this is installed, `init` and `resume` stop writing hooks and `.mcp.json` into project
folders and remove the ones they wrote before.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from seamline import paths
from seamline.hooks import settings as hook_settings

MCP_NAME = "seamline"
GLOBAL_FLAG = "--global"

Out = Callable[[str], None]
Run = Callable[[list[str]], subprocess.CompletedProcess]


def guarded_command(event: str, python: str | None = None) -> str:
    """The hook command: a POSIX shell loop that runs Seamline only inside a project."""
    python = shlex.quote(python or sys.executable)
    script = (
        'd="${PWD:-$(pwd)}"; '
        'while [ -n "$d" ]; do '
        f'if [ -f "$d/seamline.toml" ]; then exec {python} {hook_settings.MARKER} {event} '
        f"{GLOBAL_FLAG}; fi; "
        'd="${d%/*}"; '
        "done; exit 0"
    )
    return f"/bin/sh -c {shlex.quote(script)}"


def mcp_command(python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "seamline", "mcp"]


def install(python: str | None = None, out: Out = print, run: Run | None = None) -> int:
    run = run or _run
    settings = paths.claude_settings_path()
    changed = hook_settings.install_file(settings, lambda e: guarded_command(e, python))
    out(f"{'Added' if changed else 'Already had'} Seamline's hooks in {settings}")
    status = _register_mcp(python, out, run)
    out(
        "\nDone. Every Claude Code session started inside a folder with seamline.toml (at any "
        "depth) is now tracked; everywhere else the hooks exit in a few milliseconds and the "
        "MCP server offers no tools.\n"
        "Next: `seamline init` at a project's top folder (once per project). For projects set "
        "up before, run `seamline resume` there to remove their per-folder hooks and "
        ".mcp.json entry. Start new sessions (open ones keep what they started with)."
    )
    return status


def uninstall(out: Out = print, run: Run | None = None) -> int:
    run = run or _run
    settings = paths.claude_settings_path()
    if hook_settings.uninstall_file(settings, delete_if_empty=False):
        out(f"Removed Seamline's hooks from {settings}")
    else:
        out(f"No Seamline hooks in {settings}")
    if mcp_registered():
        result = _claude(run, ["mcp", "remove", "--scope", "user", MCP_NAME])
        if result is None:
            out(f"`claude` isn't on PATH; remove it yourself: claude mcp remove -s user {MCP_NAME}")
            return 1
        out("Removed the user-level seamline MCP server")
    out(
        "Projects keep their seamline.toml and ledger; run `seamline resume` in one to give it "
        "per-folder hooks again."
    )
    return 0


def hooks_installed() -> bool:
    return bool(hook_settings.installed_file(paths.claude_settings_path()))


def mcp_registered() -> bool:
    """Whether ~/.claude.json lists a user-scope seamline server (read only)."""
    try:
        data = json.loads(_claude_json().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return MCP_NAME in (data.get("mcpServers") or {})


def active() -> bool:
    """User-level mode is on when the hooks are in the user settings."""
    return hooks_installed()


def _register_mcp(python: str | None, out: Out, run: Run) -> int:
    command = mcp_command(python)
    if mcp_registered():
        _claude(run, ["mcp", "remove", "--scope", "user", MCP_NAME])  # Refresh the command
    result = _claude(run, ["mcp", "add", "--scope", "user", MCP_NAME, "--", *command])
    manual = f"claude mcp add --scope user {MCP_NAME} -- {shlex.join(command)}"
    if result is None:
        out(f"`claude` isn't on PATH; register the MCP server yourself:\n  {manual}")
        return 1
    if result.returncode != 0:
        out(f"Registering the MCP server failed: {result.stderr.strip()}\nTry:\n  {manual}")
        return 1
    out(f"Registered the seamline MCP server for all projects: {shlex.join(command)}")
    return 0


def _claude(run: Run, args: list[str]) -> subprocess.CompletedProcess | None:
    exe = shutil.which("claude")
    if exe is None:
        return None
    return run([exe, *args])


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _claude_json() -> Path:
    """Where Claude Code keeps user-scope MCP servers: ~/.claude.json, or inside
    $CLAUDE_CONFIG_DIR when that is set."""
    override = paths.claude_home()
    if override != Path.home() / ".claude":
        return override / ".claude.json"
    return Path.home() / ".claude.json"
