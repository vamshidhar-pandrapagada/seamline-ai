"""Hygiene rule (SWERepo, hygiene/changelog@1): CHANGELOG.md keeps an `[Unreleased]` section (Keep a Changelog), so
every change has a place to be recorded before a release. Standard library and pytest only.
"""

import re
from pathlib import Path

# The repository root holds .seamspec/config.toml, wherever SeamSpec keeps this file (.seamspec/proposed/<id>/ while
# proposed, .seamspec/tests/ once locked, or a copy of the staged tree).
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".seamspec" / "config.toml").is_file())
UNRELEASED = re.compile(r"^##\s*\[Unreleased\]", re.IGNORECASE | re.MULTILINE)


def test_the_changelog_keeps_an_unreleased_section():
    changelog = ROOT / "CHANGELOG.md"
    assert changelog.is_file(), "CHANGELOG.md is missing"
    assert UNRELEASED.search(changelog.read_text(encoding="utf-8", errors="replace")), "no ## [Unreleased] section"


def test_the_heading_is_found_and_only_as_a_heading():
    assert UNRELEASED.search("# Changelog\n\n## [Unreleased]\n\n- a change\n")
    assert UNRELEASED.search("## [unreleased]\n")
    assert not UNRELEASED.search("See [Unreleased] below.\n")
    assert not UNRELEASED.search("## [1.0.0] - 2026-01-01\n")
