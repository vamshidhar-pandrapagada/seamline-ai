"""The background worker: ingests sessions the hooks flagged, then exits.

- A flagged session is ingested once its transcript has been quiet for `idle_minutes`, or at
  once when it is urgent (another session of the project got a prompt, or it is about to be
  compacted or has ended).
- Only sessions seen by the hooks are touched. Older sessions are never backfilled in the
  background; `seamline ingest` does that when you ask.
- Every model call counts against `[worker] daily_budget_usd`. When the next call would pass
  it, the worker stops for the day and leaves the rest unread for tomorrow.
- One worker per project (a lock file); it exits when nothing is flagged.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from seamline import paths
from seamline.config import Config
from seamline.extract.budget import MeteredProvider
from seamline.extract.providers import LLMProvider
from seamline.ledger import queries as q
from seamline.ledger.db import open_ledger
from seamline.resolve.ingest import ingest, pending
from seamline.transcripts.discover import discover, inherited_uuids
from seamline.transcripts.sources.base import SessionSource
from seamline.transcripts.sources.claude_code import ClaudeCodeSource
from seamline.worker_control import acquire_lock, read_status, write_status

log = logging.getLogger("seamline.worker")

POLL_SECONDS = 5.0


def run_worker(
    config: Config,
    provider_factory: Callable[[], LLMProvider],
    *,
    source: SessionSource | None = None,
    poll_seconds: float = POLL_SECONDS,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    today: Callable[[], date] = date.today,
) -> int:
    root = config.root
    lock = acquire_lock(root)
    if lock is None:
        return 0  # Another worker has it
    try:
        conn = open_ledger(root)
        worker = _Worker(config, conn, provider_factory, source or ClaudeCodeSource(), today)
        if worker.over_budget():
            return 0
        while True:
            queue = [r for r in q.work_queue(conn) if r["session_id"] not in worker.skipped]
            if not queue:
                write_status(root, "idle", "nothing to do")
                return 0
            ready = [r for r in queue if r["urgent"] or _quiet(r, config, clock())]
            if not ready:
                write_status(root, "waiting", f"{len(queue)} session(s) still active")
                sleep(poll_seconds)
                continue
            write_status(root, "working", f"ingesting {len(ready)} session(s)")
            if not worker.process(ready):
                return 0
    except Exception as e:  # noqa: BLE001 (record it for `seamline status`, then exit)
        log.exception("worker failed")
        write_status(root, "error", f"{type(e).__name__}: {e}")
        return 1
    finally:
        lock.close()


class _Worker:
    def __init__(self, config, conn: sqlite3.Connection, provider_factory, source, today):
        self.config = config
        self.conn = conn
        self.provider_factory = provider_factory
        self.source = source
        self.today = today
        self.provider: MeteredProvider | None = None
        self.skipped: set[str] = set()  # Failed this run; retried by the next worker

    def over_budget(self) -> bool:
        day = self.today().isoformat()
        cap = self.config.worker.daily_budget_usd
        status = read_status(self.config.root)
        if status.get("state") == "budget" and status.get("day") == day:
            return True
        spent = q.spent_on(self.conn, day)
        if spent >= cap:
            self._budget_stop(f"daily cap reached: ${spent:.2f} of ${cap:.2f} spent today")
            return True
        return False

    def process(self, rows: list[sqlite3.Row]) -> bool:
        """Ingest these sessions. False when the worker must stop (cap, credentials)."""
        ids = {r["session_id"] for r in rows}
        sessions = discover(self.config, self.source)
        infos = [s.info for s in sessions]
        targets = [s for s in sessions if s.info.session_id in ids]
        todo = pending(
            self.conn,
            self.config,
            targets,
            self.source,
            lambda i: inherited_uuids(i, infos, self.source),
        )
        with self.conn:
            for sid in ids - {p.session.info.session_id for p in todo}:
                q.clear_flags(self.conn, sid)  # Nothing new (or not found under the project)
        for p in todo:
            sid = p.session.info.session_id
            if self.provider is None:
                self.provider = MeteredProvider(
                    self.provider_factory(),
                    self.conn,
                    self.config.extract.model,
                    cap_usd=self.config.worker.daily_budget_usd,
                    today=self.today,
                )
            result = ingest(self.conn, self.config, p, self.provider)
            report = result.report
            log.info(
                "%s %s: %d fact(s), %d/%d excerpt(s), %d error(s)",
                p.session.service,
                sid[:8],
                len(report.facts),
                report.chunks_done,
                report.chunks,
                len(report.errors),
            )
            if report.stopped:
                if self.provider.refused:
                    self._budget_stop(str(self.provider.refused))
                else:
                    write_status(self.config.root, "error", report.errors[-1])
                return False
            if not result.advanced:
                self.skipped.add(sid)
                continue
            if _grew(p.session.info.path, p.new.offset):  # Lines written while we worked
                with self.conn:
                    q.touch_session(
                        self.conn,
                        sid,
                        service=p.session.service,
                        transcript_path=str(p.session.info.path),
                        dirty=True,
                    )
        return True

    def _budget_stop(self, message: str) -> None:
        log.warning(message)
        write_status(self.config.root, "budget", message, day=self.today().isoformat())


def _quiet(row: sqlite3.Row, config: Config, now: float) -> bool:
    """No new transcript lines for `idle_minutes` (a missing file counts as quiet)."""
    try:
        mtime = Path(row["transcript_path"]).stat().st_mtime
    except OSError:
        return True
    return now - mtime >= config.worker.idle_minutes * 60


def _grew(path: Path, offset: int) -> bool:
    try:
        return Path(path).stat().st_size > offset
    except OSError:
        return False


def setup_logging(root: Path) -> None:
    path = paths.worker_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
