"""Score extraction against hand labels: precision (extracted facts that are right) and
recall (labeled facts that were found).

    uv run python eval/score_extraction.py draft eval/out/<id>.json > eval/labels/<id>.toml
    uv run python eval/score_extraction.py score eval/labels eval/out

`draft` turns an extraction result into a label file to correct by hand: delete wrong facts,
fix kinds, add facts the extractor missed. `score` matches each labels file to the result
with the same session id. A result fact matches a label when the kinds agree and the quotes
overlap (one contains the other, or they share at least half their words).
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from seamline.extract.verify import normalize


def overlaps(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    if a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def match(expected: list[dict], found: list[dict]) -> tuple[int, int]:
    """(found facts that match a label, labels matched), each label used at most once."""
    unused = list(expected)
    hits = 0
    for f in found:
        for label in unused:
            if label["kind"] == f["kind"] and overlaps(label["quote"], f["quote"]):
                unused.remove(label)
                hits += 1
                break
    return hits, len(expected) - len(unused)


def draft(result_path: str) -> None:
    result = json.loads(Path(result_path).read_text())
    print(f'session = "{result["session_id"]}"')
    print("# Keep correct facts, delete wrong ones, fix kinds, add missing ones.")
    print(f"# Drafted from {Path(result_path).name} ({result['prompt_version']}).\n")
    for f in result["facts"]:
        print("[[fact]]")
        print(f'kind = "{f["kind"]}"')
        print(f"claim = {json.dumps(f['claim'])}")
        print(f"quote = {json.dumps(f['quote'])}")
        print(f"line = {f['line']}\n")


def score(labels_dir: str, results_dir: str) -> int:
    results = {}
    for p in Path(results_dir).glob("*.json"):
        data = json.loads(p.read_text())
        results[data["session_id"]] = data
    rows, tp_found, n_found, tp_labels, n_labels = [], 0, 0, 0, 0
    for label_file in sorted(Path(labels_dir).glob("*.toml")):
        labels = tomllib.loads(label_file.read_text())
        sid = labels["session"]
        result = next((r for k, r in results.items() if k.startswith(sid)), None)
        if result is None:
            print(f"warning: no result for {label_file.name} ({sid})", file=sys.stderr)
            continue
        expected, found = labels.get("fact", []), result["facts"]
        hit_found, hit_labels = match(expected, found)
        rows.append((sid[:8], len(found), hit_found, len(expected), hit_labels))
        tp_found, n_found = tp_found + hit_found, n_found + len(found)
        tp_labels, n_labels = tp_labels + hit_labels, n_labels + len(expected)
    if not rows:
        print("nothing to score")
        return 1
    print(f"{'session':<10} {'found':>6} {'right':>6} {'labels':>7} {'recalled':>9}")
    for sid, nf, hf, nl, hl in rows:
        print(f"{sid:<10} {nf:>6} {hf:>6} {nl:>7} {hl:>9}")
    precision = tp_found / n_found if n_found else 0.0
    recall = tp_labels / n_labels if n_labels else 0.0
    print(f"\nprecision {precision:.0%}   recall {recall:.0%}   (gate: precision >= 80%)")
    return 0 if precision >= 0.8 else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "draft":
        draft(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "score":
        sys.exit(score(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(2)
