"""Guess a project's services and contract files for `seamline init`."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from seamline.config import INTEGRATION

SERVICE_MARKERS = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "Dockerfile",
)
SKIP_DIRS = frozenset(
    {"node_modules", ".venv", "venv", "vendor", "target", "dist", "build", ".git", "__pycache__"}
)
SERVICE_DEPTH = 2  # Look one or two levels below the project root
CONTRACT_DEPTH = 4

_COMPOSE_RE = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$")
_OPENAPI_RE = re.compile(r"^(openapi|swagger)([.-][\w.-]+)?\.(ya?ml|json)$", re.IGNORECASE)
_NAME_SUFFIXES = ("-service", "_service", "-svc", "_svc")


@dataclass(frozen=True)
class DetectedService:
    name: str  # Suggested name
    path: str  # Relative to the project root, POSIX style
    marker: str  # The file that gave it away


@dataclass(frozen=True)
class DetectedContract:
    path: str  # Relative to the project root, POSIX style
    kind: str  # "proto" | "compose" | "openapi"


def detect_services(root: Path | str) -> list[DetectedService]:
    """Folders one or two levels down that contain a service marker file.

    A folder that is itself a service isn't searched further: nested packages belong to it.
    """
    root = Path(root).resolve()
    found: list[tuple[str, str]] = []

    def visit(folder: Path, depth: int) -> None:
        for child in _subdirs(folder):
            marker = next((m for m in SERVICE_MARKERS if (child / m).is_file()), None)
            if marker:
                found.append((child.relative_to(root).as_posix(), marker))
            elif depth < SERVICE_DEPTH:
                visit(child, depth + 1)

    visit(root, 1)
    names = suggest_names([rel for rel, _ in found])
    return [DetectedService(n, rel, m) for n, (rel, m) in zip(names, found, strict=True)]


def detect_contracts(root: Path | str) -> list[DetectedContract]:
    """Folders holding .proto files, compose files and OpenAPI specs."""
    root = Path(root).resolve()
    proto_dirs: set[Path] = set()
    files: list[DetectedContract] = []

    for folder, dirnames, filenames in os.walk(root):
        here = Path(folder)
        depth = len(here.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if not _skip(d)) if depth < CONTRACT_DEPTH else []
        for fn in sorted(filenames):
            rel = (here / fn).relative_to(root).as_posix()
            if fn.endswith(".proto"):
                proto_dirs.add(here)
            elif _COMPOSE_RE.match(fn):
                files.append(DetectedContract(rel, "compose"))
            elif _OPENAPI_RE.match(fn):
                files.append(DetectedContract(rel, "openapi"))

    protos = [
        DetectedContract(d.relative_to(root).as_posix() or ".", "proto")
        for d in sorted({_proto_root(d, root) for d in proto_dirs})
    ]
    # Drop proto folders nested inside another detected proto folder.
    protos = [
        p for p in protos if not any(q is not p and _is_under(p.path, q.path) for q in protos)
    ]
    return protos + files


def suggest_names(paths: list[str]) -> list[str]:
    """Folder name, lowercased, minus a -service suffix and any prefix shared by all.

    ["agent-core", "agent-orchestrator"] -> ["core", "orchestrator"]. Collisions fall
    back to the full relative path with slashes turned into dashes.
    """
    bases = [_slug(Path(p).name) for p in paths]
    bases = [_strip_suffix(b) for b in bases]
    prefix = _shared_prefix(bases)
    if prefix:
        bases = [b[len(prefix) :] for b in bases]
    names: list[str] = []
    for base, path in zip(bases, paths, strict=True):
        name = base if base and bases.count(base) == 1 else _slug(path.replace("/", "-"))
        name = name or "service"
        names.append(f"{name}-service" if name == INTEGRATION else name)
    return names


def _proto_root(folder: Path, root: Path) -> Path:
    """Climb from a folder of .proto files through parents that contain only folders.

    proto/agents/*.proto -> proto (proto/ holds nothing but agents/), while
    services/orders/proto stays put because services/orders/ has its own files.
    """
    while folder.parent != root and folder != root and _only_dirs(folder.parent):
        folder = folder.parent
    return folder


def _only_dirs(folder: Path) -> bool:
    try:
        return all(e.is_dir() or e.name.startswith(".") for e in folder.iterdir())
    except OSError:
        return False


def _subdirs(folder: Path) -> list[Path]:
    try:
        return sorted(e for e in folder.iterdir() if e.is_dir() and not _skip(e.name))
    except OSError:
        return []


def _skip(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def _is_under(child: str, parent: str) -> bool:
    return parent == "." or child.startswith(parent.rstrip("/") + "/")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    return s


def _strip_suffix(name: str) -> str:
    for suffix in _NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _shared_prefix(names: list[str]) -> str:
    """A prefix ending in '-' or '_' that every name (2+) shares and none consists of."""
    if len(names) < 2:
        return ""
    common = os.path.commonprefix(names)
    cut = max(common.rfind("-"), common.rfind("_"))
    if cut < 0:
        return ""
    prefix = common[: cut + 1]
    return "" if any(n == prefix or len(n) == len(prefix) for n in names) else prefix
