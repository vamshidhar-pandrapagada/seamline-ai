"""`seamline install`: user-level hooks guarded by a seamline.toml check, a user-scope MCP
server with no tools outside projects, and per-project entries stepping aside."""

import asyncio
import io
import json
import os
import subprocess
import time

import pytest

from seamline import global_install, paths
from seamline.config import parse_config
from seamline.hooks import dispatch
from seamline.hooks import settings as hs
from seamline.lifecycle import install_hooks
from seamline.mcp_server import registration
from seamline.project_init import InitError, run_init


@pytest.fixture
def stub_python(tmp_path):
    """Stands in for Python in the hook command: records that it ran and with what."""
    log = tmp_path / "ran.log"
    stub = tmp_path / "stub-python"
    stub.write_text(f'#!/bin/sh\necho "$PWD $*" >> {log}\n')
    stub.chmod(0o755)
    return stub, log


def run_guard(event, cwd, stub):
    cmd = global_install.guarded_command(event, str(stub))
    env = {**os.environ, "PWD": str(cwd)}
    started = time.perf_counter()
    result = subprocess.run(cmd, shell=True, cwd=cwd, env=env, input="{}", text=True)
    return result.returncode, time.perf_counter() - started


def test_guard_runs_seamline_anywhere_below_a_project(tmp_path, stub_python):
    stub, log = stub_python
    deep = tmp_path / "proj" / "services" / "new-one" / "src"
    deep.mkdir(parents=True)
    (tmp_path / "proj" / "seamline.toml").write_text('project = "p"\n')
    assert run_guard("Stop", deep, stub)[0] == 0
    assert log.read_text().strip() == f"{deep} -m seamline.hooks Stop --global"


def test_guard_does_nothing_outside_projects(tmp_path, stub_python):
    stub, log = stub_python
    elsewhere = tmp_path / "other" / "deep"
    elsewhere.mkdir(parents=True)
    code, seconds = run_guard("UserPromptSubmit", elsewhere, stub)
    assert code == 0 and not log.exists()
    assert seconds < 0.5  # A shell loop; Python never starts


def test_guard_quotes_paths_with_spaces(tmp_path):
    cmd = global_install.guarded_command("Stop", "/My Tools/python")
    assert cmd.startswith("/bin/sh -c ")
    assert "'\"'\"'/My Tools/python'\"'\"'" in cmd  # quoted inside the quoted script


class FakeClaude:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd[1:])
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_install_and_uninstall_touch_only_seamline_entries(isolated_claude_home, monkeypatch):
    monkeypatch.setattr(global_install.shutil, "which", lambda name: "/usr/bin/claude")
    settings = paths.claude_settings_path()
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]},
            }
        )
    )
    claude = FakeClaude()
    assert global_install.install(python="/py", out=lambda _: None, run=claude) == 0
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine"
    assert sorted(hs.installed_file(settings)) == sorted(hs.EVENTS)
    assert global_install.active()
    assert claude.calls == [
        ["mcp", "add", "--scope", "user", "seamline", "--", "/py", "-m", "seamline", "mcp"]
    ]

    (isolated_claude_home / ".claude.json").write_text(json.dumps({"mcpServers": {"seamline": {}}}))
    global_install.uninstall(out=lambda _: None, run=claude)
    data = json.loads(settings.read_text())
    assert data == {
        "theme": "dark",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]},
    }
    assert claude.calls[-1] == ["mcp", "remove", "--scope", "user", "seamline"]
    assert not global_install.active()


def test_install_without_claude_on_path_prints_the_command(isolated_claude_home, monkeypatch):
    monkeypatch.setattr(global_install.shutil, "which", lambda name: None)
    lines = []
    assert global_install.install(python="/py", out=lines.append) == 1
    assert any(
        "claude mcp add --scope user seamline -- /py -m seamline mcp" in line for line in lines
    )
    assert global_install.hooks_installed()  # The hooks part still happened


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    (root / "services" / "orders").mkdir(parents=True)
    (root / "seamline.toml").write_text('project = "shop"\n')
    return parse_config({"project": "shop", "services": {"orders": "services/orders"}}, root=root)


def test_resume_with_user_level_install_removes_per_folder_entries(
    project, isolated_claude_home, monkeypatch
):
    install_hooks(project, lambda _: None)  # The older per-project setup
    assert hs.installed(project.root) and registration.installed(project)
    hs.install_file(paths.claude_settings_path(), global_install.guarded_command)
    lines = []
    install_hooks(project, lines.append)
    assert not hs.installed(project.root) and not hs.installed(project.root / "services/orders")
    assert not registration.installed(project)
    assert any("User-level install found" in line for line in lines)


def test_user_level_hook_steps_aside_when_the_folder_has_its_own(project, monkeypatch):
    handled = []
    monkeypatch.setattr(dispatch, "handle", lambda *a: handled.append(a) or "")
    payload = json.dumps({"session_id": "s", "cwd": str(project.root)})
    hs.install(project.root, "/py")
    dispatch.main(["Stop", "--global"], io.StringIO(payload), io.StringIO())
    assert handled == []  # The folder's own hook handles it
    dispatch.main(["Stop"], io.StringIO(payload), io.StringIO())
    assert len(handled) == 1
    hs.uninstall(project.root)
    dispatch.main(["Stop", "--global"], io.StringIO(payload), io.StringIO())
    assert len(handled) == 2  # No folder hooks: the user-level one does it


def test_mcp_server_outside_a_project_offers_no_tools():
    from seamline.mcp_server.server import build_empty_server

    assert asyncio.run(build_empty_server().list_tools()) == []


def test_init_refuses_obvious_collections(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / "Documents" / "a").mkdir(parents=True)
    with pytest.raises(InitError, match="holds many projects"):
        run_init(tmp_path / "Documents", yes=True, out=lambda _: None)
    (tmp_path / "work" / "old").mkdir(parents=True)
    (tmp_path / "work" / "old" / "seamline.toml").write_text('project = "old"\n')
    with pytest.raises(InitError, match="already is a Seamline project"):
        run_init(tmp_path / "work", yes=True, out=lambda _: None)


def test_init_notes_service_repos_but_goes_ahead(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    root = tmp_path / "system"
    for name in ("api", "web"):
        (root / name / ".git").mkdir(parents=True)
    lines = []
    run_init(root, yes=True, out=lines.append)
    assert any("2 subfolders are git repos" in line for line in lines)
    assert (root / "seamline.toml").exists()


def test_status_reports_the_user_level_install_and_leftovers(project, isolated_claude_home):
    from seamline.ledger.db import open_ledger
    from seamline.lifecycle import run_status

    hs.install(project.root, "/py")  # A leftover from the per-folder setup
    hs.install_file(paths.claude_settings_path(), global_install.guarded_command)
    lines = []
    run_status(open_ledger(project.root), project, lines.append)
    text = "\n".join(lines)
    assert "user-level install" in text
    assert "MCP server (user scope)" in text and "MISSING (seamline install)" in text
    assert "leftover per-folder entries" in text and "." in text
