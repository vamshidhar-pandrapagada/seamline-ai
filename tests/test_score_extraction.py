import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "eval" / "score_extraction.py"


def run(*args):
    return subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True)


def test_draft_then_score(tmp_path):
    out, labels = tmp_path / "out", tmp_path / "labels"
    out.mkdir()
    labels.mkdir()
    result = {
        "session_id": "abcd1234-full-id",
        "prompt_version": "v",
        "facts": [
            {
                "kind": "decision",
                "claim": "Use Redis.",
                "quote": "we will use Redis for sessions",
                "line": 3,
            },
            {
                "kind": "dead_end",
                "claim": "Sync failed.",
                "quote": "sync call timed out under load",
                "line": 9,
            },
        ],
    }
    (out / "a.json").write_text(json.dumps(result))
    drafted = run("draft", out / "a.json")
    assert drafted.returncode == 0
    # The reviewer keeps the first fact, drops the second, and adds a missed one.
    text = drafted.stdout.split("[[fact]]")
    edited = (
        text[0]
        + "[[fact]]"
        + text[1]
        + '[[fact]]\nkind = "provides"\nquote = "orders emits order.created"\n'
    )
    (labels / "abcd1234.toml").write_text(edited)
    scored = run("score", labels, out)
    assert "precision 50%" in scored.stdout
    assert "recall 50%" in scored.stdout
    assert scored.returncode == 1  # below the 80% gate
