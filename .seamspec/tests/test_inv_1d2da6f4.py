"""Hygiene rule (SWERepo, hygiene/ci-tests@1): the CI workflow still runs the test suite, and `ci-ok` still depends on
it, so an agent can't quietly weaken CI.

In `.github/workflows/swerepo-ci.yml`: a `test` job, and a `test-<project>` job for every folder in `.swerepo.toml`'s
`[projects] listed`. Each has a step that runs `uv run pytest` (`uv run` options such as `--locked` allowed) as a
command of its own at the top of the script, not masked (`|| …`, `; …`, `| …`, `… &`, `--version`, `--help`,
`--collect-only`; `&& …` only as the script's last command; folded `run: >` blocks and lines ending in a backslash are
joined first), and neither the job nor that step is switched off (`if: false`, also quoted or as `${{ false }}`) or
allowed to fail (`continue-on-error: true`, likewise). `ci-ok`'s `needs:` names each of these jobs, and `ci-ok` still
fails when one of them fails: it runs `if: always()` (a skipped required check counts as passed), isn't allowed to
fail, and has a step, neither switched off nor allowed to fail, that reads the jobs' results into a variable (`NEEDS:
${{ toJSON(needs) }}`, then `var=$(… "$NEEDS" …)` with `.result` in the script) and fails on it: `if [ -n "$var" ];
then`, then `exit` with a non-zero number before any other `exit` and before the `fi`. Either step stops at its first
failing command: GitHub's own `bash` or `sh` (not another `shell:`), no `set +e`, and no `exit` before it. Read as
text, not parsed as YAML: action pins, comments and the order of jobs don't matter. Standard library and pytest only.
"""

import re
import tomllib
from pathlib import Path

# The repository root holds .seamspec/config.toml, wherever SeamSpec keeps this file (.seamspec/proposed/<id>/ while
# proposed, .seamspec/tests/ once locked, or a copy of the staged tree).
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".seamspec" / "config.toml").is_file())
WORKFLOW = ".github/workflows/swerepo-ci.yml"
JOB = re.compile(r"^  (?P<name>[A-Za-z0-9_-]+):\s*(?:#.*)?$")
PYTEST = re.compile(r"^uv run(?: --?[A-Za-z0-9][\w-]*(?:=\S+)?)* pytest(?P<rest>(?:\s.*)?)$")
NOT_A_RUN = re.compile(r"\||;|(?<!&)&(?!&)|(?:^|\s)(?:--version|--help|-h|--collect-only|--co)(?:\s|$)")
OFF = re.compile(r"^\s*if:\s*(?:\$\{\{\s*)?[\'\"]?false[\'\"]?\s*(?:\}\})?\s*(?:#.*)?$", re.IGNORECASE)
MAY_FAIL = re.compile(
    r"^\s*continue-on-error:\s*(?:\$\{\{\s*)?[\'\"]?true[\'\"]?\s*(?:\}\})?\s*(?:#.*)?$", re.IGNORECASE
)
ALWAYS = re.compile(r"^    if:\s*(?:\$\{\{\s*)?always\(\)\s*(?:\}\})?\s*(?:#.*)?$")
NEEDS_ENV = re.compile(r"^\s*NEEDS:\s*\$\{\{\s*toJSON\(needs\)\s*\}\}\s*(?:#.*)?$")
EXIT = re.compile(r"^(?:exit|return)\b(?P<code>.*)$")
FAILS = re.compile(r"^\s*[1-9][0-9]*\s*$")  # `exit 1`: a non-zero number
ERREXIT_OFF = re.compile(r"\bset\b[^;&|]*(?:\s\+[A-Za-z]*e|\+o\s+errexit)")  # `set +e`, `set +eu`, `set +o errexit`
SHELL = re.compile(r"^\s*shell:\s*(?P<shell>.*?)\s*(?:#.*)?$")
ASSIGN = re.compile(r"^(?P<var>[A-Za-z_][A-Za-z0-9_]*)=\$\(.*\$\{?NEEDS\b")
OPENS = re.compile(r"^(?:if|for|while|until|case|select)\b|\{$")
CLOSES = re.compile(r"(?:^|;\s*)(?:fi|done|esac|\})$")
NEEDS_INLINE = re.compile(r"^    needs:\s*\[(?P<items>[^\]]*)\]")
NEEDS_BLOCK = re.compile(r"^    needs:\s*(?:#.*)?$")
NEEDS_ITEM = re.compile(r"^      -\s+(?P<name>[A-Za-z0-9_-]+)\s*(?:#.*)?$")
STEP = re.compile(r"^      - ")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _jobs(text: str) -> dict[str, list[str]]:
    """Each job's lines, by name: the jobs under `jobs:`, at two spaces."""
    jobs: dict[str, list[str]] = {}
    current, inside = None, False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            inside, current = line.rstrip().startswith("jobs:"), None
            continue
        match = JOB.match(line) if inside else None
        if match:
            current = match["name"]
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)
    return jobs


def _defaults(lines: list[str], indent: int) -> list[str]:
    """The lines of a `defaults:` key at `indent` spaces: the workflow's (0) or a job's (4)."""
    found: list[str] = []
    inside = False
    for line in lines:
        if line.strip() and _indent(line) <= indent:
            inside = _indent(line) == indent and line.strip().startswith("defaults:")
        elif inside:
            found.append(line)
    return found


def _job_keys(lines: list[str]) -> list[str]:
    """The job's own keys (four spaces), not those of its steps."""
    return [line for line in lines if _indent(line) == 4]


def _steps(lines: list[str]) -> list[list[str]]:
    """The job's steps: each from its `- ` at six spaces to the next one, or the end of `steps:`."""
    steps: list[list[str]] = []
    inside = False
    for line in lines:
        if _indent(line) <= 4:
            inside = line.strip().startswith("steps:")
            continue
        if inside and STEP.match(line):
            steps.append(["        " + line[8:]])  # `- key: value` read as the step's first key, at eight spaces
        elif inside and steps:
            steps[-1].append(line)
    return steps


def _commands(step: list[str]) -> list[str]:
    """The shell commands a step runs: `run: …`, or a `run: |` block's lines; a folded `run: >` block is one command,
    and a line ending in `\\` goes on with the next."""
    commands: list[str] = []
    for i, line in enumerate(step):
        key = line.strip()
        if _indent(line) != 8 or not key.startswith("run:"):
            continue
        value = key[4:].strip()
        if value and value[0] not in "|>":
            commands.append(value)
            continue
        block = []
        for later in step[i + 1 :]:
            if _indent(later) <= 8:
                break
            block.append(later.strip())
        if value.startswith(">"):
            block = [" ".join(block)]
        joined: list[str] = []
        for text in block:
            if joined and joined[-1].endswith("\\"):
                joined[-1] = joined[-1][:-1].rstrip() + " " + text
            else:
                joined.append(text)
        commands += joined
    return commands


def _shell_ok(lines: list[str]) -> bool:
    """No `shell:` other than GitHub's own `bash` or `sh` (both stop at the first failing command)."""
    return all(m["shell"].strip("'\"") in ("bash", "sh") for line in lines if (m := SHELL.match(line)))


def _script(step: list[str], shell_ok: bool) -> list[tuple[int, str]] | None:
    """The step's commands with their depth (0: the top of the script, 1: inside one `if`, `for` …), comments off;
    None when the step doesn't stop at a failing command: switched off, allowed to fail, another shell or `set +e`."""
    if not shell_ok or not _shell_ok(step):
        return None
    if any(_indent(line) == 8 and (OFF.match(line) or MAY_FAIL.match(line)) for line in step):
        return None
    script, depth = [], 0
    for command in _commands(step):
        line = command.split("#", 1)[0].strip()
        if ERREXIT_OFF.search(line):
            return None
        if not line:
            continue
        closes = bool(CLOSES.search(line))
        if closes and not OPENS.search(line):
            depth = max(depth - 1, 0)
        script.append((depth, line))
        if OPENS.search(line) and not closes:
            depth += 1
    return script


def _runs_the_tests(step: list[str], shell_ok: bool = True) -> bool:
    """Whether a failing test fails the step: `uv run pytest` at the top of the script, not masked, with no `exit`
    before it, and `&& …` after it only as the script's last command."""
    script = _script(step, shell_ok)
    for i, (depth, line) in enumerate(script or []):
        if depth == 0 and EXIT.match(line):
            return False
        match = PYTEST.match(line) if depth == 0 else None
        if match and not NOT_A_RUN.search(match["rest"]) and ("&&" not in match["rest"] or i == len(script) - 1):
            return True
    return False


def _judges(step: list[str], shell_ok: bool = True) -> bool:
    """Whether a step fails `ci-ok` when a needed job's result is wrong: it reads the results into a variable, then
    `if [ -n "$var" ]; then` … `exit N` (N not 0) … `fi`, with no `exit` before that one."""
    script = _script(step, shell_ok)
    if script is None or not any(NEEDS_ENV.match(line) for line in step) or ".result" not in str(script):
        return False
    var = None
    for i, (depth, line) in enumerate(script):
        if depth == 0 and (match := ASSIGN.match(line)):
            var = match["var"]
            continue
        exits = EXIT.match(line)
        if exits and (depth == 0 or var is None):
            return False  # nothing after it runs
        if var is None or depth != 0:
            continue
        if re.match(rf"^{var}=", line):
            var = None  # overwritten: no longer the results
        elif re.fullmatch(rf'if \[\[? -n "\$\{{?{var}\}}?" \]\]?; then', line):
            for later_depth, later in script[i + 1 :]:
                if later_depth == 0:
                    return False  # the `fi` came first
                if later_depth == 1 and (code := EXIT.match(later)):
                    return bool(FAILS.match(code["code"])) and later.startswith("exit")
            return False
    return False


def _needs(lines: list[str]) -> list[str]:
    for i, line in enumerate(lines):
        if match := NEEDS_INLINE.match(line):
            return [item.strip().strip("'\"") for item in match["items"].split(",") if item.strip()]
        if NEEDS_BLOCK.match(line):
            names = []
            for later in lines[i + 1 :]:  # the list ends where `needs:` ends
                item = NEEDS_ITEM.match(later)
                if item is None:
                    break
                names.append(item["name"])
            return names
    return []


def _job_name(folder: str) -> str:
    return "test-" + re.sub(r"[^a-z0-9-]+", "-", folder.lower()).strip("-")


def _expected() -> list[str]:
    try:
        state = tomllib.loads((ROOT / ".swerepo.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        state = {}
    projects = state.get("projects", {})
    listed = projects.get("listed", []) if isinstance(projects, dict) else []
    listed = listed if isinstance(listed, list) else []
    return ["test", *(_job_name(folder) for folder in listed if isinstance(folder, str))]


def _problems(text: str, expected: list[str]) -> list[str]:
    jobs, top = _jobs(text), _shell_ok(_defaults(text.splitlines(), 0))
    if "ci-ok" not in jobs:
        return ["no ci-ok job"]
    needs, problems = _needs(jobs["ci-ok"]), []
    if any(OFF.match(line) for line in _job_keys(jobs["ci-ok"])):
        problems.append("ci-ok is switched off")
    if any(MAY_FAIL.match(line) for line in _job_keys(jobs["ci-ok"])):
        problems.append("ci-ok is allowed to fail")
    if not any(ALWAYS.match(line) for line in _job_keys(jobs["ci-ok"])):
        problems.append("ci-ok doesn't run when a job fails (if: always())")
    if not any(_judges(step, top and _shell_ok(_defaults(jobs["ci-ok"], 4))) for step in _steps(jobs["ci-ok"])):
        problems.append("ci-ok doesn't fail when a needed job fails")
    for name in expected:
        lines = jobs.get(name)
        if lines is None:
            problems.append(f"no {name} job")
            continue
        keys = _job_keys(lines)
        if any(OFF.match(line) for line in keys):
            problems.append(f"{name} is switched off")
        if any(MAY_FAIL.match(line) for line in keys):
            problems.append(f"{name} is allowed to fail")
        if not any(_runs_the_tests(step, top and _shell_ok(_defaults(lines, 4))) for step in _steps(lines)):
            problems.append(f"{name} doesn't run `uv run pytest`")
        if name not in needs:
            problems.append(f"ci-ok doesn't need {name}")
    return problems


def test_ci_still_runs_the_tests_and_ci_ok_needs_them():
    workflow = ROOT / WORKFLOW
    assert workflow.is_file(), f"{WORKFLOW} is missing"
    assert _problems(workflow.read_text(encoding="utf-8", errors="replace"), _expected()) == []


SAMPLE = """name: swerepo-ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0123456789012345678901234567890123456789  # v9.9.9
        with:
          persist-credentials: false
      - run: uv sync --locked
      - run: uv run pytest
  test-services-api:
    runs-on: ubuntu-latest
    steps:
      - name: tests
        run: |
          uv sync --locked
          uv run --locked pytest -q
  ci-ok:
    if: always()
    needs: [test, test-services-api]
    runs-on: ubuntu-latest
    steps:
      - name: every job passed
        env:
          NEEDS: ${{ toJSON(needs) }}
        run: |
          wrong=$(printf '%s' "$NEEDS" | jq -r 'to_entries[] | select(.value.result != "success") | .key')
          if [ -n "$wrong" ]; then
            exit 1
          fi
"""
BOTH = ["test", "test-services-api"]


def test_the_workflow_is_read_as_text():
    assert _problems(SAMPLE, ["test", _job_name("services/API")]) == []
    assert _problems(SAMPLE.replace("      - run: uv run pytest\n", "      - run: echo skipped\n", 1), ["test"])
    assert _problems(SAMPLE.replace("needs: [test, ", "needs: ["), ["test"]) == ["ci-ok doesn't need test"]
    assert _problems(SAMPLE, ["test", "test-web"]) == ["no test-web job"]
    off = SAMPLE.replace("  test:\n", "  test:\n    if: false\n", 1)
    assert _problems(off, ["test"]) == ["test is switched off"]
    may_fail = SAMPLE.replace("  test:\n", "  test:\n    continue-on-error: true\n", 1)
    assert _problems(may_fail, ["test"]) == ["test is allowed to fail"]


def test_a_test_step_switched_off_or_masked_doesnt_count():
    step = "      - run: uv run pytest\n"
    for weakened in (
        "      - run: uv run pytest\n        if: false\n",
        "      - if: ${{ false }}\n        run: uv run pytest\n",
        "      - run: uv run pytest\n        continue-on-error: true\n",
        "      - run: uv run pytest || true\n",
        "      - run: uv run pytest; true\n",
        "      - run: uv run pytest --version\n",
        "      - run: uv run pytest --collect-only\n",
        "      - run: uv run pytest --help\n",
        "      - run: uv run pytest -h\n",
        "      - run: uv run pytest --co\n",
        "      - run: echo uv run pytest\n",
        "      - run: uv run pytest &\n",
        "      - if: 'false'\n        run: uv run pytest\n",
        "      - if: False\n        run: uv run pytest\n",
        "      - continue-on-error: ${{ true }}\n        run: uv run pytest\n",
        "      - run: >\n          uv run pytest\n          || true\n",
        "      - run: |\n          uv run pytest \\\n            --collect-only\n",
        "      - run: |\n          set +e\n          uv run pytest\n          echo done\n",  # the step goes on
        "      - run: |\n          uv run pytest && echo passed\n          echo finished\n",
        "      - run: uv run pytest | tee pytest.log\n",  # no pipefail: tee's status counts
        "      - run: |\n          exit 0\n          uv run pytest\n",
        "      - run: |\n          if false; then\n            uv run pytest\n          fi\n",
        "      - run: uv run pytest\n        shell: bash {0}\n",  # no -e
    ):
        assert _problems(SAMPLE.replace(step, weakened, 1), ["test"]) == ["test doesn't run `uv run pytest`"], weakened
    assert _problems(SAMPLE.replace(step, "      - run: uv run --locked pytest -x\n", 1), ["test"]) == []
    joined = "      - run: |\n          uv run pytest \\\n            -q\n"
    assert _problems(SAMPLE.replace(step, joined, 1), ["test"]) == []
    assert _problems(SAMPLE.replace(step, "      - run: uv run pytest && echo done\n", 1), ["test"]) == []
    last = "      - run: |\n          uv sync\n          uv run pytest && echo passed\n"
    assert _problems(SAMPLE.replace(step, last, 1), ["test"]) == []
    assert _problems(SAMPLE.replace(step, "      - run: uv run pytest\n        shell: bash\n", 1), ["test"]) == []


def test_a_shell_set_for_the_job_or_the_workflow_counts():
    no_e = "    defaults:\n      run:\n        shell: bash {0}\n"
    job = SAMPLE.replace("  test:\n    runs-on: ubuntu-latest\n", "  test:\n" + no_e + "    runs-on: ubuntu-latest\n")
    assert _problems(job, ["test"]) == ["test doesn't run `uv run pytest`"]
    folder = "    defaults:\n      run:\n        working-directory: services/api\n"
    assert _problems(SAMPLE.replace("  test:\n", "  test:\n" + folder, 1), ["test"]) == []
    workflow = SAMPLE.replace("jobs:\n", "defaults:\n  run:\n    shell: bash {0}\njobs:\n")
    assert _problems(workflow, ["test"]) == [
        "ci-ok doesn't fail when a needed job fails",
        "test doesn't run `uv run pytest`",
    ]


def test_ci_ok_itself_switched_off_or_allowed_to_fail():
    head = "  ci-ok:\n    if: always()\n"
    off = SAMPLE.replace(head, "  ci-ok:\n    if: ${{ false }}\n")
    assert _problems(off, BOTH) == ["ci-ok is switched off", "ci-ok doesn't run when a job fails (if: always())"]
    allowed = "  ci-ok:\n    if: always()\n    continue-on-error: 'true'\n"
    assert _problems(SAMPLE.replace(head, allowed), BOTH) == ["ci-ok is allowed to fail"]
    assert _problems(SAMPLE.replace(head, "  ci-ok:\n    if: ${{ always() }}\n"), BOTH) == []


def test_ci_ok_still_fails_when_a_job_fails():
    gate = "      - name: every job passed\n"
    judge = SAMPLE[SAMPLE.index(gate) :]
    cases = {
        "echo ok": SAMPLE.replace(judge, "      - run: echo ok\n"),
        "no exit": SAMPLE.replace("            exit 1\n", "            echo failed\n"),
        "exit masked": SAMPLE.replace("            exit 1\n", "            echo exit 1\n"),
        "no results": SAMPLE.replace("          NEEDS: ${{ toJSON(needs) }}\n", "          NEEDS: '{}'\n"),
        "step off": SAMPLE.replace(gate, gate + "        if: false\n"),
        "step may fail": SAMPLE.replace(gate, gate + "        continue-on-error: true\n"),
        "never true": SAMPLE.replace('if [ -n "$wrong" ]; then', "if false; then"),
        "exit 0 first": SAMPLE.replace("          wrong=$(", "          exit 0\n          wrong=$("),
        "exit 0 inside": SAMPLE.replace("            exit 1\n", "            exit 0\n            exit 1\n"),
        "overwritten": SAMPLE.replace("          if [ -n", '          wrong=""\n          if [ -n'),
        "errexit off": SAMPLE.replace("          wrong=$(", "          set +e\n          wrong=$("),
        "another shell": SAMPLE.replace(gate, gate + "        shell: python\n"),
    }
    for case, text in cases.items():
        assert _problems(text, BOTH) == ["ci-ok doesn't fail when a needed job fails"], case
    no_always = SAMPLE.replace("  ci-ok:\n    if: always()\n", "  ci-ok:\n")
    assert _problems(no_always, BOTH) == ["ci-ok doesn't run when a job fails (if: always())"]


def test_a_needs_list_ends_where_it_ends():
    listed = "    needs:\n      - test\n      - test-services-api\n"
    assert _problems(SAMPLE.replace("    needs: [test, test-services-api]\n", listed), BOTH) == []
    later = "    needs:\n      - test-services-api\n    runs-on: ubuntu-latest\n    steps:\n      - test\n"
    block = SAMPLE.replace("    needs: [test, test-services-api]\n    runs-on: ubuntu-latest\n    steps:\n", later)
    assert _problems(block, BOTH) == ["ci-ok doesn't need test"]  # `- test` under steps isn't in needs
