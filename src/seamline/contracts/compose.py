"""Read docker-compose files: which service listens on which ports and reads which env vars."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ComposeService:
    name: str
    line: int
    build_context: str | None  # Relative to the compose file, as written
    ports: list[str] = field(default_factory=list)  # Container ports
    env: list[str] = field(default_factory=list)  # Variable names only, never values
    depends_on: list[str] = field(default_factory=list)


def parse_compose(text: str) -> list[ComposeService]:
    try:
        root = yaml.compose(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return []
    lines = _service_lines(root)
    out = []
    for name, spec in data["services"].items():
        spec = spec if isinstance(spec, dict) else {}
        out.append(
            ComposeService(
                name=str(name),
                line=lines.get(str(name), 1),
                build_context=_build_context(spec.get("build")),
                ports=[p for p in (_container_port(x) for x in spec.get("ports") or []) if p],
                env=_env_names(spec.get("environment")),
                depends_on=_depends(spec.get("depends_on")),
            )
        )
    return out


def parse_compose_file(path: Path) -> list[ComposeService]:
    return parse_compose(path.read_text(encoding="utf-8", errors="replace"))


def _service_lines(root) -> dict[str, int]:
    """1-based line of each service's key, from the YAML node tree."""
    if root is None or not isinstance(root, yaml.MappingNode):
        return {}
    for key, value in root.value:
        if key.value == "services" and isinstance(value, yaml.MappingNode):
            return {k.value: k.start_mark.line + 1 for k, _ in value.value}
    return {}


def _build_context(build) -> str | None:
    if isinstance(build, str):
        return build
    if isinstance(build, dict) and isinstance(build.get("context"), str):
        return build["context"]
    return None


def _container_port(entry) -> str | None:
    if isinstance(entry, dict):  # long syntax
        target = entry.get("target")
        return str(target) if target is not None else None
    text = str(entry).split("/")[0]  # drop /tcp
    return text.rsplit(":", 1)[-1] or None


def _env_names(env) -> list[str]:
    if isinstance(env, dict):
        return [str(k) for k in env]
    if isinstance(env, list):
        return [str(e).split("=", 1)[0] for e in env]
    return []


def _depends(dep) -> list[str]:
    if isinstance(dep, dict):
        return [str(k) for k in dep]
    if isinstance(dep, list):
        return [str(d) for d in dep]
    return []
