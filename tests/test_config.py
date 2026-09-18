from pathlib import Path

import pytest

from seamline.config import (
    DEFAULT_MODEL,
    ConfigError,
    dump_config,
    find_config,
    find_project,
    load_config,
    parse_config,
)


def write(root: Path, text: str) -> Path:
    path = root / "seamline.toml"
    path.write_text(text)
    return path


def test_find_config_walks_up(tmp_path):
    write(tmp_path, 'project = "shop"\n')
    deep = tmp_path / "services" / "orders" / "src"
    deep.mkdir(parents=True)
    assert find_config(deep) == (tmp_path / "seamline.toml").resolve()
    assert find_project(deep).root == tmp_path.resolve()


def test_find_config_from_a_file_path(tmp_path):
    write(tmp_path, 'project = "shop"\n')
    f = tmp_path / "main.py"
    f.write_text("")
    assert find_config(f) == (tmp_path / "seamline.toml").resolve()


def test_find_config_none_outside_project(tmp_path):
    assert find_config(tmp_path) is None
    assert find_project(tmp_path) is None


def test_nearest_config_wins(tmp_path):
    write(tmp_path, 'project = "outer"\n')
    inner = tmp_path / "inner"
    inner.mkdir()
    write(inner, 'project = "inner"\n')
    (inner / "src").mkdir()
    assert find_project(inner / "src").project == "inner"


def test_defaults(tmp_path):
    cfg = load_config(write(tmp_path, 'project = "shop"\n'))
    assert cfg.services == {}
    assert cfg.contracts == []
    assert cfg.extract.provider == "anthropic"
    assert cfg.extract.model == DEFAULT_MODEL
    assert cfg.worker.idle_minutes == 10
    assert cfg.brief.max_tokens == 400
    assert cfg.brief.update_max_tokens == 100


def test_full_config(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
project = "shop"
[services]
web = "apps/web"
api = "apps/api/"
[contracts]
paths = ["proto", "docker/docker-compose.yml"]
[extract]
provider = "anthropic"
model = "claude-sonnet-5"
[worker]
idle_minutes = 5
[brief]
max_tokens = 300
update_max_tokens = 50
""",
        )
    )
    assert cfg.services == {"web": "apps/web", "api": "apps/api"}
    assert cfg.contracts == ["proto", "docker/docker-compose.yml"]
    assert cfg.extract.model == "claude-sonnet-5"
    assert cfg.worker.idle_minutes == 5
    assert cfg.service_dirs()["api"] == (tmp_path / "apps/api").resolve()


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "project"),
        ('project = ""', "project"),
        ('project = "x"\ntypo = 1', "unknown key"),
        ('project = "x"\n[services]\nWeb = "web"', "lowercase"),
        ('project = "x"\n[services]\nintegration = "a"', "reserved"),
        ('project = "x"\n[services]\na = "/abs/path"', "relative"),
        ('project = "x"\n[services]\na = "../sibling"', "relative"),
        ('project = "x"\n[services]\na = "."', "project root"),
        ('project = "x"\n[services]\na = "web"\nb = "web/"', "share the path"),
        ('project = "x"\n[services]\na = 3', "path string"),
        ('project = "x"\nservices = "nope"', "table"),
        ('project = "x"\n[contracts]\npaths = "proto"', "list of strings"),
        ('project = "x"\n[extract]\nprovider = "openai"', "provider"),
        ('project = "x"\n[extract]\ntemperature = 0', "unknown key"),
        ('project = "x"\n[worker]\nidle_minutes = 0', "positive integer"),
        ('project = "x"\n[worker]\nidle_minutes = true', "positive integer"),
        ('project = "x"\n[brief]\nmax_tokens = "400"', "positive integer"),
        ("project = [", "invalid TOML"),
    ],
)
def test_invalid_configs(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_dump_round_trips(tmp_path):
    cfg = parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders"},
            "contracts": {"paths": ["proto"]},
        },
        root=tmp_path,
    )
    again = load_config(write(tmp_path, dump_config(cfg)))
    assert again == cfg
