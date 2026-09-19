"""Start the background worker and ask whether it's running. Imported by hooks, so it stays
light (no model SDK, no transcript parsing).

One worker per project at a time: it holds an exclusive lock on `.seamline/worker.lock` for
as long as it runs, so the lock disappears with the process even if it crashes.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from seamline import paths

INTERNAL_ENV = "SEAMLINE_INTERNAL"  # Set for processes Seamline starts; their hooks do nothing


def acquire_lock(root: Path):
    """The open lock file if this process now owns the worker lock, else None."""
    path = paths.worker_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 (held for the worker's lifetime)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def worker_running(root: Path) -> bool:
    handle = acquire_lock(root)
    if handle is None:
        return True
    handle.close()  # Closing releases the lock
    return False


def spawn_worker(root: Path, python: str | None = None) -> None:
    """Start `seamline worker` detached from the hook, so the hook returns at once."""
    log = paths.worker_log_path(root)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as err:
        subprocess.Popen(
            [python or sys.executable, "-m", "seamline", "worker", "--root", str(root)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
            env={**os.environ, INTERNAL_ENV: "1"},
        )


def write_status(root: Path, state: str, message: str = "", **extra) -> None:
    data = {
        "state": state,
        "message": message,
        "pid": os.getpid(),
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
        **extra,
    }
    path = paths.worker_status_path(root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def read_status(root: Path) -> dict:
    try:
        data = json.loads(paths.worker_status_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
