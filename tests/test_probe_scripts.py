"""Phase 1 hook experiment scripts: never fail, stay silent outside projects, log no content."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")


def run(args, stdin="", env=None):
    return subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, **(env or {})},
    )


def log_hook(tmp_path, event, payload):
    log = tmp_path / "log.jsonl"
    r = run([SCRIPTS / "hook_logger.sh", event], payload, {"SEAMLINE_PROBE_LOG": str(log)})
    return r, log


def test_logger_outside_project_is_silent(tmp_path):
    payload = json.dumps({"session_id": "s", "cwd": str(tmp_path), "prompt": "hi"})
    r, log = log_hook(tmp_path, "UserPromptSubmit", payload)
    assert r.returncode == 0 and r.stdout == ""
    entry = json.loads(log.read_text())
    assert entry["event"] == "UserPromptSubmit"
    assert entry["seamline_root"] == ""
    assert entry["payload"]["session_id"] == "s"


def test_logger_never_records_content(tmp_path):
    payload = json.dumps(
        {
            "session_id": "s",
            "cwd": str(tmp_path),
            "hook_event_name": "Stop",
            "prompt": "my secret plan",
            "last_assistant_message": "the answer is 42",
            "tool_input": {"command": "cat .env"},
            "items": [1, 2],
            "stop_hook_active": False,
        }
    )
    _, log = log_hook(tmp_path, "Stop", payload)
    text = log.read_text()
    for content in ("secret plan", "answer is 42", "cat .env"):
        assert content not in text
    p = json.loads(text)["payload"]
    assert p["prompt"] == "<14 chars>"
    assert p["last_assistant_message"] == "<16 chars>"
    assert p["tool_input"] == "<object: command>"
    assert p["items"] == "<array of 2>"
    assert p["stop_hook_active"] is False
    assert p["hook_event_name"] == "Stop"
    assert p["cwd"] == str(tmp_path)


def test_logger_inside_project_prints_probe(tmp_path):
    (tmp_path / "seamline.toml").write_text('project = "p"\n')
    (tmp_path / "svc").mkdir()
    payload = json.dumps({"session_id": "s", "cwd": str(tmp_path / "svc"), "source": "startup"})
    r, log = log_hook(tmp_path, "SessionStart", payload)
    assert r.returncode == 0
    assert "lighthouse" in r.stdout
    assert json.loads(log.read_text())["seamline_root"] == str(tmp_path)


def test_logger_survives_garbage(tmp_path):
    r, log = log_hook(tmp_path, "Stop", "not json")
    assert r.returncode == 0
    assert json.loads(log.read_text())["payload"] == "<not JSON: 8 chars>"


def test_user_install_uninstall_round_trip(tmp_path):
    original = {
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other.sh"}]}]},
    }
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(original))
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert run([SCRIPTS / "probe_hooks.sh", "install"], env=env).returncode == 0
    installed = json.loads(settings.read_text())
    assert len(installed["hooks"]["Stop"]) == 2
    assert "SessionStart" in installed["hooks"]
    assert list(tmp_path.glob("settings.json.bak.*"))
    assert run([SCRIPTS / "probe_hooks.sh", "uninstall"], env=env).returncode == 0
    assert json.loads(settings.read_text()) == original


def test_project_install_leaves_user_settings_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{}")
    project = tmp_path / "shop"
    project.mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(home)}
    r = run([SCRIPTS / "probe_hooks.sh", "install", "--project", str(project)], env=env)
    assert r.returncode == 0, r.stderr
    local = project / ".claude" / "settings.local.json"
    assert set(json.loads(local.read_text())["hooks"]) >= {"SessionStart", "Stop"}
    assert (home / "settings.json").read_text() == "{}"
    assert not list(home.glob("*.bak.*"))
    r = run([SCRIPTS / "probe_hooks.sh", "uninstall", "--project", str(project)], env=env)
    assert r.returncode == 0, r.stderr
    assert not (project / ".claude").exists()  # install created it, so uninstall removes it


def test_project_install_keeps_existing_local_settings(tmp_path):
    project = tmp_path / "shop"
    (project / ".claude").mkdir(parents=True)
    local = project / ".claude" / "settings.local.json"
    existing = {"permissions": {"allow": ["Bash(ls)"]}}
    local.write_text(json.dumps(existing))
    run([SCRIPTS / "probe_hooks.sh", "install", "--project", str(project)])
    run([SCRIPTS / "probe_hooks.sh", "uninstall", "--project", str(project)])
    assert json.loads(local.read_text()) == existing
