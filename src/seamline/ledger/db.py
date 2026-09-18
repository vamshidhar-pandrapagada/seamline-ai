"""Open the project's ledger, applying numbered migrations on the way.

`PRAGMA user_version` records the last migration applied. WAL mode lets hooks read while
the worker writes.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

from seamline import paths

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def migrations() -> list[tuple[int, str]]:
    folder = resources.files("seamline.ledger").joinpath("migrations")
    found = []
    for entry in folder.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text()))
    return sorted(found)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) and migrate a ledger database."""
    db_path = Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _migrate(conn)
    return conn


def open_ledger(root: Path) -> sqlite3.Connection:
    return connect(paths.ledger_path(root))


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in migrations():
        if version <= current:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
