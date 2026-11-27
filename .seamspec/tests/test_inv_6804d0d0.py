"""Hygiene rule (SWERepo, hygiene/adr-index@1): every ADR file is listed in the ADR index, and the index lists no ADR
without a file.

The ADR folder is `[adopted] adr_dir` in `.swerepo.toml`, else `docs/adr`; its README.md holds the index between
`<!-- swerepo:adr-index:begin -->` and `<!-- swerepo:adr-index:end -->`, as `swerepo adr index` writes it: one table row
per ADR, its first cell a link `[ADR-YYYYMMDD-xxxx](ADR-YYYYMMDD-xxxx-title.md)` to the file. Only those links count:
an ID named in a title or a "SUPERSEDED by" cell doesn't list an ADR. Standard library and pytest only.
"""

import re
import tomllib
from pathlib import Path

# The repository root holds .seamspec/config.toml, wherever SeamSpec keeps this file (.seamspec/proposed/<id>/ while
# proposed, .seamspec/tests/ once locked, or a copy of the staged tree).
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".seamspec" / "config.toml").is_file())
ADR_FILE = re.compile(r"^(?P<id>ADR-\d{8}-[0-9a-f]{4})(?:-[A-Za-z0-9._-]+)?\.md$")
ROW = re.compile(r"^\s*\|\s*\[(?P<id>ADR-\d{8}-[0-9a-f]{4})\]\((?P<file>[^)\s]+)\)\s*\|")
BEGIN = re.compile(r"^\s*<!--\s*swerepo:adr-index:begin(?:\s(?:(?!-->).)*)?-->\s*$")
END = re.compile(r"^\s*<!--\s*swerepo:adr-index:end(?:\s(?:(?!-->).)*)?-->\s*$")


def _folder(state_text: str | None) -> str:
    """The ADR folder `.swerepo.toml` names (`[adopted] adr_dir`), else `docs/adr`."""
    try:
        state = tomllib.loads(state_text or "")
    except tomllib.TOMLDecodeError:
        return "docs/adr"
    adopted = state.get("adopted", {})
    folder = adopted.get("adr_dir") if isinstance(adopted, dict) else None
    return folder if isinstance(folder, str) and folder else "docs/adr"


def _adr_dir() -> str:
    try:
        return _folder((ROOT / ".swerepo.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return "docs/adr"


def _index(readme: str) -> str | None:
    """The text between the index markers, or None when there is no single, closed index."""
    lines = readme.splitlines()
    begins = [i for i, line in enumerate(lines) if BEGIN.match(line)]
    ends = [i for i, line in enumerate(lines) if END.match(line)]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return None
    return "\n".join(lines[begins[0] + 1 : ends[0]])


def _problems(index: str, files: set[str]) -> list[str]:
    """What is wrong with an index against the ADR files on disk (their names)."""
    rows = [m for line in index.splitlines() if (m := ROW.match(line))]
    linked = [m["file"] for m in rows]
    problems = [f"{m['file']}: linked as {m['id']}" for m in rows if not m["file"].startswith(m["id"])]
    problems += [f"{name}: listed twice" for name in sorted({n for n in linked if linked.count(n) > 1})]
    problems += [f"{name}: missing from the index" for name in sorted(files - set(linked))]
    problems += [f"{name}: listed without a file" for name in sorted(set(linked) - files)]
    return problems


def test_every_adr_is_in_the_index_and_nothing_else():
    folder = ROOT / _adr_dir()
    readme = folder / "README.md"
    assert readme.is_file(), f"{_adr_dir()}/README.md is missing"
    index = _index(readme.read_text(encoding="utf-8", errors="replace"))
    assert index is not None, f"{_adr_dir()}/README.md has no single ADR index between its markers"
    files = {p.name for p in folder.iterdir() if p.is_file() and ADR_FILE.match(p.name)}
    assert _problems(index, files) == [], "run `swerepo adr index`"


A = "ADR-20260101-aaaa-first.md"
B = "ADR-20260102-bbbb-second.md"
TABLE = (
    "| ID | Title | Status |\n|---|---|---|\n"
    f"| [ADR-20260101-aaaa]({A}) | First | SUPERSEDED by ADR-20260102-bbbb |\n"
    f"| [ADR-20260102-bbbb]({B}) | Second, after ADR-20260101-aaaa | ACCEPTED |\n"
)


def test_the_index_is_read_from_each_rows_link_only():
    readme = f"# ADRs\nADR-20260103-cccc is mentioned outside.\n<!-- swerepo:adr-index:begin -->\n{TABLE}"
    readme += "<!-- swerepo:adr-index:end -->\n"
    assert _problems(_index(readme) or "", {A, B}) == []
    assert _index("# ADRs\n<!-- swerepo:adr-index:begin -->\n") is None  # never closed
    without_b = TABLE.replace(f"| [ADR-20260102-bbbb]({B}) | Second, after ADR-20260101-aaaa | ACCEPTED |\n", "")
    assert _problems(without_b, {A, B}) == [f"{B}: missing from the index"]  # named in A's status: not listed
    assert _problems(TABLE, {A}) == [f"{B}: listed without a file"]
    assert _problems(TABLE.replace(f"({B})", "(ADR-20260102-bbbb-old-name.md)"), {A, B}) == [
        f"{B}: missing from the index",
        "ADR-20260102-bbbb-old-name.md: listed without a file",
    ]
    assert _problems(TABLE.replace(f"[ADR-20260102-bbbb]({B})", f"[ADR-20260102-bbbb]({A})"), {A, B}) == [
        f"{A}: linked as ADR-20260102-bbbb",
        f"{A}: listed twice",
        f"{B}: missing from the index",
    ]
    assert ADR_FILE.match("ADR-20260101-a1b2-use-postgres.md")
    assert not ADR_FILE.match("README.md") and not ADR_FILE.match("ADR-2026-x.md")


def test_the_adr_folder_comes_from_swerepo_toml():
    assert _folder('[adopted]\nadr_dir = "doc/decisions"\n') == "doc/decisions"
    assert _folder('[adopted]\nprogress_dir = "docs/changes"\n') == "docs/adr"
    assert _folder(None) == "docs/adr" and _folder("not = [toml") == "docs/adr"
    assert _folder('[adopted]\nadr_dir = ""\n') == "docs/adr"
