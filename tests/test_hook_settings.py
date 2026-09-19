import json

import pytest

from seamline.config import parse_config
from seamline.hooks import settings as hs
from seamline.lifecycle import install_hooks, run_pause, run_remove, run_resume

PY = "/opt/py/bin/python3"


def test_install_preserves_other_settings_and_is_idempotent(tmp_path):
    path = hs.settings_path(tmp_path)
    path.parent.mkdir()
    theirs = {"type": "command", "command": "echo mine"}
    path.write_text(
        json.dumps(
            {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [{"hooks": [theirs]}]}}
        )
    )
    assert hs.install(tmp_path, PY)
    assert not hs.install(tmp_path, PY)  # Already there: no rewrite
    data = json.loads(path.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["hooks"]["Stop"][0]["hooks"] == [theirs]
    ours = data["hooks"]["SessionStart"][0]["hooks"][0]
    assert ours["command"] == f"{PY} -m seamline.hooks SessionStart"
    assert sorted(hs.installed(tmp_path)) == sorted(hs.EVENTS)

    assert hs.uninstall(tmp_path)
    data = json.loads(path.read_text())
    assert data == {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"Stop": [{"hooks": [theirs]}]},
    }
    assert hs.installed(tmp_path) == []


def test_uninstall_deletes_a_file_only_seamline_used(tmp_path):
    hs.install(tmp_path, PY)
    assert hs.uninstall(tmp_path)
    assert not (tmp_path / ".claude").exists()
    assert not hs.uninstall(tmp_path)


def test_python_path_with_spaces_is_quoted():
    assert hs.command_for("Stop", "/My Tools/python") == "'/My Tools/python' -m seamline.hooks Stop"


def test_broken_settings_file_is_not_overwritten(tmp_path):
    path = hs.settings_path(tmp_path)
    path.parent.mkdir()
    path.write_text("{not json")
    with pytest.raises(hs.SettingsError, match="not valid JSON"):
        hs.install(tmp_path, PY)
    assert path.read_text() == "{not json"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    for s in ("orders", "payments"):
        (root / "services" / s).mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "seamline.toml").write_text('project = "shop"\n')
    return parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders", "payments": "services/payments"},
        },
        root=root,
    )


def test_install_covers_root_and_services_and_gitignores(project):
    lines = []
    install_hooks(project, lines.append)
    for folder in (
        project.root,
        project.root / "services/orders",
        project.root / "services/payments",
    ):
        assert hs.installed(folder), folder
    assert hs.GITIGNORE_ENTRY in (project.root / ".gitignore").read_text()


def test_resume_drops_hooks_of_removed_services(project):
    install_hooks(project, lambda _: None)
    project.services.pop("payments")
    lines = []
    run_resume(project, lines.append)
    assert not hs.installed(project.root / "services/payments")
    assert any("no longer a service" in line for line in lines)


def test_pause_resume_remove(project):
    install_hooks(project, lambda _: None)
    run_pause(project, lambda _: None)
    assert not hs.installed(project.root)
    assert (project.root / ".seamline" / "paused").exists()
    run_resume(project, lambda _: None)
    assert hs.installed(project.root)
    assert not (project.root / ".seamline" / "paused").exists()
    run_remove(project, ask=lambda _: "n", out=lambda _: None)
    assert not hs.installed(project.root / "services/orders")
    assert not (project.root / "seamline.toml").exists()
    assert (project.root / ".seamline").exists()  # Kept unless you say yes
