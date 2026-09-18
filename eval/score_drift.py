"""Score drift detection on planted cases: every planted mismatch caught, no false alarms.

    uv run python eval/score_drift.py [eval/drift_cases.toml]

Each case runs in a fresh in-memory ledger through the real storage and detection code.
Cases marked `known_limitation` are reported separately and don't gate. Exit status 0
only when every other case is right (the Phase 3 gate).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from seamline.ledger.db import connect
from seamline.resolve.drift import recompute
from seamline.resolve.matcher import interface_id_for
from seamline.resolve.supersede import Candidate, store_fact

DEFAULT = Path(__file__).with_name("drift_cases.toml")


def run_case(case: dict) -> bool:
    """True if drift detection flagged this case."""
    conn = connect(":memory:")
    interface_id = interface_id_for(conn, "order.created", "event")
    consumer = "orders" if case.get("same_service") else "payments"
    for kind, service, details in (
        ("provides", "orders", case["provides"]),
        ("assumes", consumer, case["assumes"]),
    ):
        store_fact(
            conn,
            Candidate(
                kind=kind,
                origin="session",
                service=service,
                attributed_by="folder",
                interface_id=interface_id,
                claim=f"{service} {kind} order.created",
                details=[{"name": k, "value": v} for k, v in details.items()],
                confidence=0.9,
                quote=f"{service} {kind}",
                session_id=service,
                line_no=1,
            ),
        )
    flagged = bool(recompute(conn).opened)
    conn.close()
    return flagged


def score(path: Path = DEFAULT) -> tuple[int, int, list[str], list[str]]:
    """(gated cases, right, wrong, known-limitation cases still wrong)."""
    cases = tomllib.loads(path.read_text())["case"]
    wrong, known = [], []
    gated = [c for c in cases if not c.get("known_limitation")]
    for case in cases:
        if run_case(case) != case["mismatch"]:
            what = "missed" if case["mismatch"] else "false alarm"
            (known if case.get("known_limitation") else wrong).append(f"{what}: {case['name']}")
    return len(gated), len(gated) - len(wrong), wrong, known


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    total, right, wrong, known = score(path)
    gated = [c for c in tomllib.loads(path.read_text())["case"] if not c.get("known_limitation")]
    planted = sum(c["mismatch"] for c in gated)
    print(
        f"{right}/{total} cases right ({planted} planted mismatches, {total - planted} clean pairs)"
    )
    for w in wrong:
        print(f"  ✗ {w}")
    if known:
        print(f"known limitations still wrong ({len(known)}):")
        for k in known:
            print(f"  · {k}")
    sys.exit(0 if not wrong else 1)
