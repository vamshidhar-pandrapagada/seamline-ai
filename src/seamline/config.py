"""Load and validate `seamline.toml`, and find it by walking up from any folder."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import tomli_w

CONFIG_FILENAME = "seamline.toml"
INTEGRATION = "integration"  # Reserved scope: the project root and any unlisted folder
PROVIDERS = ("anthropic",)
# Opus 5: on two real multi-service sessions it reached ~85% precision where Haiku 4.5 got
# ~40%, at about 4-5 cents per excerpt.
DEFAULT_MODEL = "claude-opus-5"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(Exception):
    """seamline.toml is missing, unreadable or invalid."""


@dataclass
class ExtractSettings:
    provider: str = "anthropic"
    model: str = DEFAULT_MODEL


@dataclass
class WorkerSettings:
    idle_minutes: int = 10
    daily_budget_usd: float = 5.0  # Background extraction stops for the day at this spend


@dataclass
class BriefSettings:
    max_tokens: int = 400
    update_max_tokens: int = 100


@dataclass
class Config:
    project: str
    root: Path
    services: dict[str, str] = field(default_factory=dict)  # name -> path relative to root
    contracts: list[str] = field(default_factory=list)  # paths relative to root
    extract: ExtractSettings = field(default_factory=ExtractSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    brief: BriefSettings = field(default_factory=BriefSettings)

    @property
    def path(self) -> Path:
        return self.root / CONFIG_FILENAME

    def service_dirs(self) -> dict[str, Path]:
        """Absolute folder for each service."""
        return {name: (self.root / rel).resolve() for name, rel in self.services.items()}


def find_config(start: Path | str) -> Path | None:
    """Return the nearest seamline.toml in `start` or any parent, or None."""
    here = Path(start).resolve()
    if here.is_file():
        here = here.parent
    for folder in (here, *here.parents):
        candidate = folder / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def find_project(start: Path | str) -> Config | None:
    """Load the config of the project containing `start`, or None if it isn't in one."""
    path = find_config(start)
    return load_config(path) if path else None


def load_config(path: Path | str) -> Config:
    path = Path(path).resolve()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    return parse_config(data, root=path.parent)


def parse_config(data: dict, root: Path) -> Config:
    """Validate raw TOML data. Unknown keys are errors, so typos don't pass silently."""
    _check_keys(data, {"project", "services", "contracts", "extract", "worker", "brief"}, "")

    project = data.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ConfigError("`project` must be a non-empty string")

    services = _parse_services(data.get("services", {}))

    contracts_table = _table(data, "contracts")
    _check_keys(contracts_table, {"paths"}, "contracts")
    contracts = contracts_table.get("paths", [])
    if not isinstance(contracts, list) or not all(isinstance(p, str) for p in contracts):
        raise ConfigError("`contracts.paths` must be a list of strings")
    for p in contracts:
        _check_relative(p, "contracts.paths")

    extract_table = _table(data, "extract")
    _check_keys(extract_table, {"provider", "model"}, "extract")
    extract = ExtractSettings(**extract_table)
    if extract.provider not in PROVIDERS:
        raise ConfigError(f"`extract.provider` must be one of {', '.join(PROVIDERS)}")
    if not isinstance(extract.model, str) or not extract.model:
        raise ConfigError("`extract.model` must be a non-empty string")

    worker_table = _table(data, "worker")
    _check_keys(worker_table, {"idle_minutes", "daily_budget_usd"}, "worker")
    worker = WorkerSettings(**worker_table)
    _check_positive_int(worker.idle_minutes, "worker.idle_minutes")
    budget = worker.daily_budget_usd
    if isinstance(budget, bool) or not isinstance(budget, int | float) or budget <= 0:
        raise ConfigError("`worker.daily_budget_usd` must be a positive number of US dollars")
    worker.daily_budget_usd = float(budget)

    brief_table = _table(data, "brief")
    _check_keys(brief_table, {"max_tokens", "update_max_tokens"}, "brief")
    brief = BriefSettings(**brief_table)
    _check_positive_int(brief.max_tokens, "brief.max_tokens")
    _check_positive_int(brief.update_max_tokens, "brief.update_max_tokens")

    return Config(
        project=project.strip(),
        root=Path(root).resolve(),
        services=services,
        contracts=contracts,
        extract=extract,
        worker=worker,
        brief=brief,
    )


def dump_config(config: Config) -> str:
    """Render a Config as seamline.toml text, with a short explanatory header."""
    data = {
        "project": config.project,
        "services": dict(config.services),
        "contracts": {"paths": list(config.contracts)},
        "extract": {"provider": config.extract.provider, "model": config.extract.model},
        "worker": {
            "idle_minutes": config.worker.idle_minutes,
            "daily_budget_usd": config.worker.daily_budget_usd,
        },
        "brief": {
            "max_tokens": config.brief.max_tokens,
            "update_max_tokens": config.brief.update_max_tokens,
        },
    }
    header = (
        "# Seamline project config. Commit this file; .seamline/ stays git-ignored.\n"
        "# [services] maps a name to a folder (relative to this file). The longest matching\n"
        "# folder wins; the project root and unlisted folders count as the integration scope.\n"
        "# [contracts] lists files or folders that define interfaces\n"
        "# (.proto, docker-compose, OpenAPI).\n\n"
    )
    return header + tomli_w.dumps(data)


def _parse_services(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError('`services` must be a table of name = "path"')
    services: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    for name, rel in raw.items():
        if not _NAME_RE.match(name):
            raise ConfigError(
                f"service name {name!r} must be lowercase letters, digits, '-' or '_'"
            )
        if name == INTEGRATION:
            raise ConfigError(f"{INTEGRATION!r} is reserved for the project root scope")
        if not isinstance(rel, str):
            raise ConfigError(f"services.{name} must be a path string")
        norm = _check_relative(rel, f"services.{name}")
        if norm == ".":
            raise ConfigError(
                f"services.{name} points at the project root, which is the integration scope"
            )
        if norm in seen_paths:
            raise ConfigError(f"services {seen_paths[norm]!r} and {name!r} share the path {norm!r}")
        seen_paths[norm] = name
        services[name] = norm
    return services


def _check_relative(rel: str, where: str) -> str:
    """Require a path that stays inside the project root; return it normalized."""
    p = PurePosixPath(rel.replace("\\", "/"))
    if not rel.strip() or p.is_absolute() or ".." in p.parts:
        raise ConfigError(f"`{where}` must be a relative path inside the project, got {rel!r}")
    return p.as_posix()


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"`{key}` must be a table")
    return value


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        prefix = f"[{where}] " if where else ""
        raise ConfigError(f"{prefix}unknown key(s): {', '.join(unknown)}")


def _check_positive_int(value: object, where: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"`{where}` must be a positive integer")
