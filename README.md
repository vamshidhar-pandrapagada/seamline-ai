# Seamline

Shared memory across the Claude Code sessions of one project, focused on how the project's
services fit together: which service provides which interface, who consumes it, what each
side assumes about it, plus the decisions made and the dead ends hit along the way. Its
standout feature is **contract drift detection**: flagging when two services' sessions (or a
session and the code) disagree about the same interface.

> **Status: Phases 0–3 done.** Seamline reads your Claude Code sessions, extracts facts
> into a per-project ledger, scans `.proto` and docker-compose contracts, and detects drift,
> all **on demand** (you run `seamline ingest`). Automatic capture and briefs in your
> sessions arrive in Phase 4; tools Claude can query arrive in Phase 5 (see
> [Roadmap](#roadmap)).

## How it works

![Seamline architecture: Claude Code sessions are read, facts are extracted with Claude Opus 5 and kept in a local SQLite ledger with facts from the code, and mismatches between services are flagged. Next: briefs via Claude Code hooks and an MCP server.](docs/architecture.png)

Solid parts work today; dashed parts are next. Everything runs on your machine except
extraction, which sends session excerpts (secrets masked) to the Anthropic API.

```
Claude Code transcripts (~/.claude/projects/…)          proto / docker-compose files
        │                                                          │
        ▼                                                          ▼
 read new lines → mask secrets → keep only your prompts       seamline scan
 and Claude's replies (tool output is never sent)                  │
        │                                                          │
        ▼                                                          │
 Claude extracts typed facts, each with a verbatim quote           │
 (quotes not found in the transcript are discarded)                │
        │                                                          │
        ▼                                                          ▼
 attribute each fact to a service → store in .seamline/ledger.db (SQLite)
        │
        ▼
 duplicates become evidence · newer facts replace older ones · facts the code no longer
 backs go stale · drift: consumer assumptions vs provider facts (sessions and code)
```

Fact kinds: `provides`, `consumes`, `assumes` (a specific claim about someone else's
interface), `decision`, `dead_end` (something tried that failed). Interface kinds: `grpc`,
`http`, `event`, `topic`, `table`, `env`, `config`, `cli`.

## Requirements

- macOS or Linux, Python 3.12, [uv](https://docs.astral.sh/uv/)
- Claude Code (desktop app or CLI): Seamline reads the transcripts it already saves
- An Anthropic API key for extraction (`ANTHROPIC_API_KEY`, from console.anthropic.com).
  Seamline never stores it. A Claude subscription login is not used.

## Install

```bash
git clone https://github.com/vamshidhar-pandrapagada/seamline-ai.git
uv tool install --editable ./seamline-ai   # puts `seamline` on your PATH
seamline --version
```

`--editable` runs the code straight from the clone, so pulling changes needs no reinstall
(reinstall only when dependencies change). Remove with `uv tool uninstall seamline`.
Nothing is added to `~/.claude/settings.json` or to any other project.

## Set up a project

Run once, at the project root (the folder that contains all its services):

```bash
cd ~/code/my-project
seamline init          # interactive; --yes accepts everything detected
```

`init`:

- **Detects services**: folders one or two levels down containing `package.json`,
  `pyproject.toml`, `Cargo.toml`, `go.mod`, `pom.xml` or a `Dockerfile` (skipping
  `node_modules`, `.venv`, `target`, `dist`, `build`, …). A shared prefix is dropped from
  suggested names (`agent-core` → `core`). Press Enter to keep a name, type a new one, or
  `-` to drop it.
- **Detects contracts**: folders with `.proto` files, `docker-compose*.yml` /
  `compose*.yml`, and OpenAPI files (listed, but not scanned yet).
- **Writes** `seamline.toml` (commit it if you like) and `.seamline/` (the ledger and logs),
  and adds `.seamline/` to `.gitignore` if the folder is a git repo.
- **Warns** if Claude Code's `cleanupPeriodDays` is unset or low (transcripts are deleted
  after ~30 days by default; 365 is a good value) and reports how many sessions it found.

Folders without a build file (e.g. a `skills/` folder of Markdown) aren't proposed. Anything
not listed belongs to the **integration** scope; add a service by hand any time:

```toml
[services]
skills = "skills"
```

Run `init` only once per project, at the root; it refuses to create a project nested
inside another. Only projects you `init` are ever read.

### Where to open sessions

Either way works:

- **In a service folder** (`my-project/payments`): the session's facts default to that
  service.
- **At the project root**, telling Claude which part to work on: facts are attributed one
  by one (see [Attribution](#attribution)). `seamline sessions` lists such sessions as
  `integration`; their facts still land on the right services.

## Everyday use

From anywhere inside the project:

```bash
seamline scan      # read the [contracts] files into facts from code (run again after they change)
seamline ingest    # extract facts from sessions with new lines; shows sessions, model calls
                   # and estimated cost, and asks before calling the model
seamline facts     # what the ledger knows, grouped by kind, with the quote each came from
seamline drift     # open contract mismatches, with a quote from each side
```

Real `drift` output from the `examples/shop` demo (extracted with prompt v3, which still
let the model name the field `field_amount`; v6 asks for the literal field name, and
matching ignores the stray prefix either way):

```
OPEN MISMATCHES (1)
  ✗ `order.created` `field_amount`: payments assumes field_amount (numeric, dollars not cents),
    but orders doesn't provide it (it has `amount_cents`)
      provides orders    c7c9bdc4 L35: "order.created event contract guarantees three fields: …"
      assumes  payments  077c8737 L4: "payload gives us order_id and amount, where amount is the order total …"
```

`drift` exits with status 1 while mismatches are open (0 when clean), so scripts and CI can
react.

## Commands

| Command | What it does | Options |
|---|---|---|
| `seamline init [PATH]` | Set up a project (above) | `-y/--yes` accept all; `--force` overwrite `seamline.toml` |
| `seamline sessions` | The project's sessions by service, size, activity, and how much is ingested | `--root DIR` (works on folders without `init`) |
| `seamline show <session>` | A session's events labeled `keep` / `anchor` / `evidence` / `skip` (a unique id prefix is enough) | `--all` include skipped; `--full` untruncated; `--root` |
| `seamline extract <session>` | **Dry run**: extract and print facts, store nothing | `--model`, `--max-chunks N`, `--json FILE` (save the result, e.g. for `eval/`), `--debug` (raw model reply), `--root` |
| `seamline ingest [session]` | Extract new session lines into the ledger | `-y/--yes` skip the cost prompt; `--redo` re-extract that session from the start with the current prompt and rules; `--max-chunks N`; `--model`; `--root` |
| `seamline facts` | List facts with sources (session facts; code facts hidden) | `--service X`, `--kind K`, `--code` (only facts from code), `--all` (include superseded/stale, with the reason) |
| `seamline scan` | Read `[contracts]` into facts from code | `--root` |
| `seamline drift` | Check facts against the code, recompute and list mismatches | `--root` |

`ingest`, `facts`, `scan` and `drift` need a project that has run `init`; they never create
`.seamline/` elsewhere. `brief`, `worker`, `hook`, `pause`, `resume`, `remove`, `status`
(Phase 4) and `mcp` (Phase 5) are listed in `--help` but not implemented yet.

## Configuration: `seamline.toml`

```toml
project = "agent-platform"

[services]                        # name = folder, relative to this file
core = "agent-core"             # the longest matching folder wins; unlisted folders and
orchestrator = "agent-orchestrator"  # the root are the "integration" scope

[contracts]                       # files or folders scanned by `seamline scan`
paths = ["proto", "docker/docker-compose.yml"]

[extract]
provider = "anthropic"            # the only provider
model = "claude-opus-5"           # default; claude-haiku-4-5 is cheaper but less precise

[worker]
idle_minutes = 10                 # Phase 4
daily_budget_usd = 5.0            # background extraction stops for the day at this spend

[brief]
max_tokens = 400                  # Phase 4
update_max_tokens = 100
```

Unknown keys are errors, so typos don't pass silently. Service names use lowercase letters,
digits, `-` and `_`; `integration` is reserved.

## How facts stay accurate

**Extraction.** Sessions are split into turn-aligned excerpts (~6k tokens). Only your
prompts and Claude's replies are sent; files Claude touched appear as context lines like
`(Claude edited services/orders/events.py)`. The model fills a strict schema through a
forced tool call. Every fact must quote one line of the transcript word for word (only
formatting may differ); otherwise it's discarded. The prompt is versioned
(`src/seamline/extract/prompts/extract_facts.md`, currently v6) and records only what was
said, never the extractor's own judgments.

<a id="attribution"></a>**Attribution** (which service a fact is about), in order:

1. the service the extractor set because the text names it;
2. files the fact's own text mentions that the session touched (`client.py`,
   `agent-core/src/grpc/…`);
3. a service or folder name in the fact's text;
4. files touched around the statement: tool calls since Claude last spoke, then until it
   next speaks, then the whole turn (paths in `Read`/`Edit`/`Write` and in Bash commands);
5. the folder the session started in; otherwise the integration scope.

**Storing.** Interface names are normalized (`order.created` = `OrderCreated` =
`ORDER_CREATED`; `POST /orders/{id}` = `post /orders/:orderId`; a proto message and its
package-qualified name are one interface). A repeated fact becomes extra evidence on the
existing one. A newer statement on the same subject replaces the older one: rules decide
clear cases (a full restatement); ambiguous pairs (renames like `amount` → `total_cents`,
look-alike fields like `created_at` / `updated_at`, similar decisions) go to a small LLM
judge that answers "does the newer fact replace the older one?", keeping both when unsure.
Facts from sessions and facts from code never replace each other.

**Staleness.** A session fact is marked stale when its service's code changed *after* the
statement and a field it names is no longer used in that code (as a quoted key, attribute or
declaration, in snake/camel/Pascal case). It's restored if the field comes back. Unchanged
code isn't evidence: the contract may simply not be written yet.

**Drift.** Each active `assumes` fact is compared with the `provides` facts for the same
interface from other services and from code (code is ground truth). Flagged: a typed field
the consumer assumes that the provider doesn't have (with a hint: "it has `amount_cents`"),
and the same field with conflicting types. Only details that name a type take part, so
free-form notes never raise alarms. Mismatches resolve by themselves when either side is
fixed or goes stale.

**Resumed sessions.** Resuming a session copies its history into a new transcript; copied
records are recognized and extracted only once.

## Privacy and cost

- **What leaves your machine:** excerpts of your prompts and Claude's replies (after
  masking API keys, tokens, passwords, private keys, URL credentials, and `NAME=value`
  secrets; the content of `.env` files Claude read is masked entirely), sent to the
  Anthropic API with your key. Tool output is never sent.
- **What stays local:** the ledger (`.seamline/ledger.db`, git-ignored), logs, and the
  original transcripts (Seamline never modifies them).
- **Cost:** on Claude Opus 5, about 4–5 cents per excerpt (measured); a typical session is
  1–3 excerpts. The judge's calls are rare and small. `ingest` shows an estimate and asks
  before spending; `--yes` skips the question.
- **Spend tracking:** every model call's cost (the token usage the API reports × list
  prices) is recorded in the ledger. `ingest` shows today's total against
  `[worker] daily_budget_usd` ($5 by default). The cap stops background work (Phase 4);
  runs you confirm yourself are recorded but not blocked. For a hard limit on the key
  itself, also set a spend limit in the Anthropic Console.

## Known limitations

- **Claude Code only.** Other IDEs' histories aren't read (the reader sits behind a
  source-adapter interface for later).
- **Contracts:** only `.proto` and docker-compose are scanned; OpenAPI is detected but not
  read. Compose env vars are recorded by name only, never their values.
- **Drift** misses a unit stated only in the value (`integer milliseconds` vs `integer
  seconds`) and raises a false alarm when a consumer flattens a nested field (`customer`
  vs `customer_id`). Both are tracked in `eval/drift_cases.toml`.
- **Attribution:** a `cd` inside a Bash command isn't followed.
- **Staleness** relies on file modification times (a fresh clone or branch switch makes all
  code look newer), makes a whole fact stale when one field is gone, and counts a field
  named in a comment as used.
- **Extraction precision** was measured on a small real sample: 80–93% on two multi-service
  sessions with Opus 5 (vs ~40% with Haiku 4.5). Keep an eye on it; statements of a state
  that the same session then fixed can still slip through as current facts.
- Subagent transcripts are counted but not read.
- **Nothing is automatic yet:** until Phase 4, run `seamline ingest` yourself.

## Roadmap

- **Phase 4: hooks and background worker.** `init` writes per-project hooks (in
  `.claude/settings.local.json` at the root and in each service folder; nothing
  user-wide). A background worker ingests a session when it goes quiet, or right away when
  you switch to another session of the same project, within a daily spending cap. New
  sessions start with a brief (≤ 400 tokens, open mismatches first); open sessions get a
  short note on their next prompt when another session changed something relevant.
  `pause` / `resume` / `remove` per project; `status` for a health check.
- **Phase 5: MCP tools** Claude can call for detail (`get_integration_context`,
  `check_contract`, `find_dead_ends`, `search_history`, `remember`), catching up on
  unprocessed session lines before answering.
- **Phase 6:** two weeks of real use, a Claude Code plugin if it can be enabled per
  project, and the demo.

## Development

```bash
uv sync
uv run pytest                                     # ~240 tests, no network
uv run ruff check . && uv run ruff format --check .
```

- **Evaluation** (separate from tests; calls the real model):
  [`eval/README.md`](eval/README.md). `eval/score_extraction.py` drafts label files from an
  `extract --json` result and scores precision/recall against your corrected labels;
  `eval/score_drift.py` runs the planted drift cases (currently 22/22, plus 2 known
  limitations reported separately).
- **Demo:** `examples/shop` (orders, payments, notifications) with a planted mismatch:
  orders emits `amount_cents`, payments reads `amount`.
- **Findings from real data:** [`docs/transcript-format.md`](docs/transcript-format.md)
  (how Claude Code stores sessions) and [`docs/hook-behavior.md`](docs/hook-behavior.md)
  (which hooks fire in the desktop app, and why hooks are per project).
- **Hook experiment scripts** (Phase 1): `scripts/probe_hooks.sh install --project DIR |
  uninstall | report`, which log hook calls without recording any conversation content.

## License

[MIT](LICENSE)
