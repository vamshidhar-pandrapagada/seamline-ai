"""`seamline install` / `seamline uninstall-global`: one user-level setup, so every session
started anywhere inside a Seamline project is tracked, including folders added later.

Hooks go into your Claude Code user settings (`~/.claude/settings.json`). Each one is a
small shell guard: it walks up from the session's folder looking for `seamline.toml` and
exits at once (no Python, nothing read or recorded) when there is none.

The MCP server is not installed here: `seamline init` registers it in each project's own
`.mcp.json`, so it only ever starts in Seamline projects. With this installed, `init` and
`resume` stop writing per-folder hooks and remove the ones they wrote before.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable

from seamline import paths
from seamline.hooks import settings as hook_settings

GLOBAL_FLAG = "--global"

Out = Callable[[str], None]


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


def install(python: str | None = None, out: Out = print) -> int:
    settings = paths.claude_settings_path()
    changed = hook_settings.install_file(settings, lambda e: guarded_command(e, python))
    out(f"{'Added' if changed else 'Already had'} Seamline's hooks in {settings}")
    out(
        "\nDone. Every Claude Code session started inside a folder with seamline.toml (at any "
        "depth) is now tracked; everywhere else the hooks exit in a few milliseconds.\n"
        "Next: `seamline init` at a project's top folder (once per project; it also turns on "
        "Seamline's MCP tools there). For projects set up before, run `seamline resume` there "
        "to remove their per-folder hooks. Start new sessions (open ones keep what they "
        "started with)."
    )
    return 0


def uninstall(out: Out = print) -> int:
    settings = paths.claude_settings_path()
    if hook_settings.uninstall_file(settings, delete_if_empty=False):
        out(f"Removed Seamline's hooks from {settings}")
    else:
        out(f"No Seamline hooks in {settings}")
    out(
        "Projects keep their seamline.toml, ledger and MCP server; run `seamline resume` in "
        "one to give it per-folder hooks again."
    )
    return 0


def hooks_installed() -> bool:
    return bool(hook_settings.installed_file(paths.claude_settings_path()))


def active() -> bool:
    """User-level mode is on when the hooks are in the user settings."""
    return hooks_installed()
