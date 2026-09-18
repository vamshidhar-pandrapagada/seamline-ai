"""`seamline init`: detect services and contracts, confirm them, write seamline.toml."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from seamline import paths
from seamline.config import CONFIG_FILENAME, INTEGRATION, Config, dump_config, find_config
from seamline.detect import DetectedContract, DetectedService, detect_contracts, detect_services
from seamline.transcripts.discover import discover

RECOMMENDED_CLEANUP_DAYS = 365
LOW_CLEANUP_DAYS = 90
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

Ask = Callable[[str], str]


class InitError(Exception):
    pass


@dataclass
class InitResult:
    config: Config
    warnings: list[str]


def run_init(
    root: Path,
    *,
    yes: bool = False,
    force: bool = False,
    ask: Ask = input,
    out: Callable[[str], None] = print,
) -> InitResult:
    root = root.resolve()
    if not root.is_dir():
        raise InitError(f"{root} is not a folder")
    target = root / CONFIG_FILENAME
    if target.exists() and not force:
        raise InitError(f"{target} already exists (use --force to overwrite)")
    parent_config = find_config(root.parent)
    if parent_config and not force:
        raise InitError(
            f"{root} is already inside the Seamline project at {parent_config.parent} "
            "(use --force to create a nested project anyway)"
        )

    services = detect_services(root)
    contracts = detect_contracts(root)

    out(f"Seamline init in {root}\n")
    if yes:
        _print_detected(services, contracts, out)
        chosen_services = {s.name: s.path for s in services}
        chosen_contracts = [c.path for c in contracts]
    else:
        chosen_services = _confirm_services(services, ask, out)
        chosen_contracts = _confirm_contracts(contracts, ask, out)

    config = Config(
        project=_project_name(root),
        root=root,
        services=chosen_services,
        contracts=chosen_contracts,
    )
    target.write_text(dump_config(config), encoding="utf-8")
    paths.logs_dir(root).mkdir(parents=True, exist_ok=True)
    out(f"\nWrote {CONFIG_FILENAME} and {paths.DATA_DIRNAME}/")

    warnings: list[str] = []
    gitignore_note = _ensure_gitignored(root)
    if gitignore_note:
        out(gitignore_note)

    warnings += _cleanup_warnings()
    warnings += _session_report(config, out)
    for w in warnings:
        out(f"warning: {w}")
    return InitResult(config=config, warnings=warnings)


def _print_detected(
    services: list[DetectedService], contracts: list[DetectedContract], out: Callable[[str], None]
) -> None:
    if services:
        out("Services:")
        for s in services:
            out(f"  {s.name:<20} {s.path}/  ({s.marker})")
    else:
        out("Services: none detected; everything will be in the integration scope.")
    if contracts:
        out("Contracts:")
        for c in contracts:
            out(f"  {c.kind:<8} {c.path}")
    else:
        out("Contracts: none detected.")


def _confirm_services(
    detected: list[DetectedService], ask: Ask, out: Callable[[str], None]
) -> dict[str, str]:
    if not detected:
        out("No services detected; everything will be in the integration scope.")
        out("You can add services to seamline.toml later.\n")
        return {}
    out("Detected services. Press Enter to keep a name, type a new one, or '-' to drop it.")
    chosen: dict[str, str] = {}
    for s in detected:
        while True:
            answer = ask(f"  {s.path}/  ({s.marker})  name [{s.name}]: ").strip()
            if answer == "-":
                break
            name = answer or s.name
            if not _NAME_RE.match(name) or name == INTEGRATION:
                out("    use lowercase letters, digits, '-' or '_' ('integration' is reserved)")
                continue
            if name in chosen:
                out(f"    {name!r} is already used by {chosen[name]}/")
                continue
            chosen[name] = s.path
            break
    out("")
    return chosen


def _confirm_contracts(
    detected: list[DetectedContract], ask: Ask, out: Callable[[str], None]
) -> list[str]:
    if not detected:
        out("No contract files detected (.proto, docker-compose, OpenAPI).\n")
        return []
    out("Detected contract files (scanned later as ground truth).")
    kept = []
    for c in detected:
        answer = ask(f"  {c.kind:<8} {c.path}  keep? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            kept.append(c.path)
    return kept


def _project_name(root: Path) -> str:
    return root.name


def _ensure_gitignored(root: Path) -> str | None:
    """Add .seamline/ to .gitignore when there is one or the folder is a git repo."""
    gitignore = root / ".gitignore"
    entry = f"{paths.DATA_DIRNAME}/"
    if not gitignore.exists() and not (root / ".git").exists():
        return f"Not a git repo: remember to keep {entry} out of version control."
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if lines & {entry, paths.DATA_DIRNAME, f"/{entry}", f"/{paths.DATA_DIRNAME}"}:
        return None
    sep = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(f"{existing}{sep}{entry}\n", encoding="utf-8")
    return f"Added {entry} to .gitignore"


def _cleanup_warnings() -> list[str]:
    days = paths.read_cleanup_period_days()
    fix = (
        f'set "cleanupPeriodDays": {RECOMMENDED_CLEANUP_DAYS} in {paths.claude_settings_path()} '
        "so older sessions stay available for backfill"
    )
    if days is None:
        return [f"Claude Code deletes transcripts inactive for ~30 days by default; {fix}."]
    if days < LOW_CLEANUP_DAYS:
        return [f"Claude Code deletes transcripts after {days} days (cleanupPeriodDays); {fix}."]
    return []


def _session_report(config: Config, out: Callable[[str], None]) -> list[str]:
    counts = Counter(s.service for s in discover(config))
    out("\nClaude Code sessions found:")
    for name in (INTEGRATION, *config.services):
        out(f"  {name:<20} {counts[name]}")
    if not counts:
        return [
            "no Claude Code sessions found for this project yet. The ledger will start "
            "from the contract scan and fill up as you work."
        ]
    return []
