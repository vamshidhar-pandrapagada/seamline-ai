"""Hygiene rule (SWERepo, hygiene/privacy@1): no personal email addresses or home-folder paths in tracked files.

Allowed (SWERepo spec §6.4): GitHub noreply addresses, reserved example domains, `git@` in SSH remotes, emails in
pyproject.toml's [project] authors and maintainers, emails in the security policy (SECURITY.md, .github/SECURITY.md
or docs/SECURITY.md: the reporting contact), placeholder home
folders such as /Users/me or /home/user, any line marked with `swerepo: allow-privacy`, and the files or folders
listed under `[privacy] allow_paths` in `.swerepo.toml` (git's glob rules: `*` within a folder, `**` across folders).
A finding names the file, the line and the kind, never what was found. Standard library and pytest only.
"""

import fnmatch
import re
import subprocess
import tomllib
from pathlib import Path

# The repository root holds .seamspec/config.toml, wherever SeamSpec keeps this file (.seamspec/proposed/<id>/ while
# proposed, .seamspec/tests/ once locked, or a copy of the staged tree).
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".seamspec" / "config.toml").is_file())

MARKER = "swerepo: allow-privacy"
EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
NOREPLY_DOMAINS = ("users.noreply.github.com", "noreply.github.com")
EXAMPLE_DOMAIN = re.compile(r"(^|\.)example(\.(com|org|net))?$")
# Backslashes may be escaped (JSON, TOML, Python strings); a user name starts with a letter, digit or "_".
HOME = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\+Users\\+)([A-Za-z0-9_][A-Za-z0-9._-]*)")
PLACEHOLDER_USERS = {"me", "user", "you", "username", "name", "runner", "runneradmin", "Shared"}
POLICY_PATHS = {"security.md", ".github/security.md", "docs/security.md"}  # where GitHub reads a security policy
MAX_BYTES = 2_000_000  # larger files are data, not text a person wrote
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}


def _parts(glob: str) -> list[str]:
    parts: list[str] = []
    for part in glob.replace("\\", "/").split("/"):
        if part in ("", ".") or (part == "**" and parts[-1:] == ["**"]):
            continue
        parts.append(part)
    return parts


def _glob_matches(glob: str, path: str) -> bool:
    """Whether `path`, or a folder holding it, matches `glob`: `*` and `?` within one folder, `**` across any."""
    parts, names = _parts(glob), [p for p in path.split("/") if p not in ("", ".")]
    if not parts:
        return False
    end = len(parts)

    def closure(states: set[int]) -> set[int]:
        done = set(states)
        for state in sorted(states):
            while state < end and parts[state] == "**":
                state += 1
                done.add(state)
        return done

    states = closure({0})
    for name in names:
        if end in states:
            return True  # a folder that matches: everything inside it too
        moved: set[int] = set()
        for state in states:
            if state == end:
                continue
            if parts[state] == "**":
                moved.add(state)
            elif re.fullmatch(fnmatch.translate(parts[state]), name):
                moved.add(state + 1)
        states = closure(moved)
        if not states:
            return False
    return end in states


def _allow_paths() -> list[str]:
    try:
        state = tomllib.loads((ROOT / ".swerepo.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    privacy = state.get("privacy", {})
    globs = privacy.get("allow_paths", []) if isinstance(privacy, dict) else []
    return [g for g in globs if isinstance(g, str)]


def _tracked_files() -> list[Path]:
    """Tracked files. Inside a git checkout ask git; in a copy of the staged tree (no .git) every file is staged."""
    if (ROOT / ".git").exists():
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
        names = out.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        return [ROOT / name for name in names if name]
    files = []
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if SKIP_DIRS.intersection(parts) or parts[:2] == (".seamspec", ".local"):
            continue
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return files


def _text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:  # listed by git but deleted in the working tree
        return None
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def _declared_emails() -> set[str]:
    """Emails the project publishes on purpose: pyproject.toml's [project] authors and maintainers."""
    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    if not isinstance(project, dict):
        return set()
    people = [p for key in ("authors", "maintainers") if isinstance(project.get(key), list) for p in project[key]]
    return {p["email"].lower() for p in people if isinstance(p, dict) and isinstance(p.get("email"), str)}


def _allowed_email(match: re.Match[str], line: str, declared: set[str]) -> bool:
    local, domain = match.group(1), match.group(2).lower()
    if f"{local}@{domain}".lower() in declared:
        return True
    if any(domain == d or domain.endswith("." + d) for d in NOREPLY_DOMAINS) or EXAMPLE_DOMAIN.search(domain):
        return True
    ssh_remote = line[match.end() : match.end() + 1] == ":" or line[: match.start()].endswith("ssh://")
    return local == "git" and ssh_remote


def _line_kinds(rel: str, line: str, declared: set[str]) -> list[str]:
    if MARKER in line:
        return []
    kinds = []
    if rel.lower() not in POLICY_PATHS:
        kinds += ["email address" for m in EMAIL.finditer(line) if not _allowed_email(m, line, declared)]
    kinds += ["home-folder path" for user in HOME.findall(line) if user.rstrip(".") not in PLACEHOLDER_USERS]
    return kinds


def _findings() -> list[str]:
    declared, allowed = _declared_emails(), _allow_paths()
    found = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if any(_glob_matches(glob, rel) for glob in allowed):
            continue
        text = _text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            found += [f"{rel}:{number}: {kind}" for kind in _line_kinds(rel, line, declared)]
    return found


def test_no_personal_emails_or_home_paths_in_tracked_files():
    assert _findings() == []  # where and what kind only, never the text itself


def test_the_scan_flags_personal_data_and_allows_the_exceptions():
    # Samples are assembled at run time, so this file never contains them literally.
    at, users, home = "@", "/" + "Users/", "/" + "home/"
    flagged = [
        "alice" + at + "mail.test.com",
        "contact: alice" + at + "evilnoreply.github.com",
        "git" + at + "mail.test.com wrote",
        users + "alice/project",
        home + "alice/.config",
        '"C:\\\\' + 'Users\\\\alice\\\\project"',
    ]
    allowed = [
        "someone" + at + "users.noreply.github.com",
        "you" + at + "example.org",
        "git" + at + "github.com:owner/repo.git",
        "ssh://git" + at + "github.com/owner/repo",
        users + "me/code",
        "see " + home + "user.",
        "alice" + at + "mail.test.com  # " + MARKER,
        "maintainer" + at + "declared.test.com",
    ]
    declared = {"maintainer" + at + "declared.test.com"}
    for line in flagged:
        assert _line_kinds("README.md", line, declared), line
    for line in allowed:
        assert not _line_kinds("README.md", line, declared), line
    for policy in ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md"):
        assert not _line_kinds(policy, "report to alice" + at + "mail.test.com", declared)
        assert _line_kinds(policy, users + "alice", declared)
    assert _line_kinds("docs/notes/SECURITY.md", "alice" + at + "mail.test.com", declared)


def test_allow_paths_read_as_git_globs():
    assert _glob_matches("tests/fixtures/**", "tests/fixtures/a/b.txt")
    assert _glob_matches("tests/fixtures", "tests/fixtures/b.txt")  # a folder: everything inside it
    assert _glob_matches("docs/*.md", "docs/notes.md")
    assert not _glob_matches("docs/*.md", "docs/sub/notes.md")  # `*` stays within one folder
    assert _glob_matches("**/*.snap", "a/b/c.snap")
    assert not _glob_matches("", "README.md")
