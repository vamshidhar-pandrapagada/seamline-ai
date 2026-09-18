# Claude Code transcript format (as observed)

Findings from Phase 1, from 10 real sessions (≈12,000 lines) recorded by the **Claude desktop app**
(`entrypoint: "claude-desktop"`), September 2026. Parsing lives in
`src/seamline/transcripts/models.py`; if the format changes, that's the file to update.
The synthetic fixture `tests/fixtures/transcripts/basic.jsonl` mirrors every shape below.

## Where sessions live

```
~/.claude/projects/<encoded cwd>/
├── <session id>.jsonl              # the transcript: one JSON record per line
├── <session id>/subagents/agent-<id>.jsonl (+ .meta.json)   # Agent tool sidechains
├── <session id>/tool-results/…     # large tool outputs stored outside the transcript
└── memory/                         # Claude's auto-memory (not a transcript)
```

- `<encoded cwd>` is the session's starting folder with every non-alphanumeric character
  replaced by `-`. **This is lossy**: `/Users/me/my_app` is stored under
  `-Users-me-my-app`. So discovery treats the folder name only as a pre-filter (a
  subfolder's encoded name always starts with its parent's) and trusts the `cwd` recorded
  inside the transcript.
- `$CLAUDE_CONFIG_DIR` overrides `~/.claude`.

## Records

Every line has a `type`. Counts from the sample:

| type | share | what it is | Seamline |
|---|---|---|---|
| `assistant` | 27% | One content block per line (`text`, `thinking` or `tool_use`); lines from one API response share `message.id` | text → keep; tool_use → anchor/evidence; thinking → skip |
| `user` | 14% | Typed prompts **and** tool results (see below) | prompts → keep; results → evidence |
| `attachment` | 16% | Context injected by the harness: token reminders, env/date, hook output (`hook_additional_context`), queued task notifications | skip |
| `system` | 3% | `stop_hook_summary`, `compact_boundary` | skip |
| others | 40% | `queue-operation`, `last-prompt`, `ai-title`, `custom-title`, `mode`, `bridge-session`, `file-history-snapshot`, … (no `uuid`, no content) | skip |

Common fields on conversational records: `uuid`, `parentUuid`, `sessionId`, `timestamp` (ISO,
UTC), `cwd`, `gitBranch`, `isSidechain`, `entrypoint`, `version`.

### Telling typed prompts from everything else (all are `type: "user"`)

| Case | How to recognize it |
|---|---|
| Typed prompt | `message.content` is a **string**; the record has `promptSource`/`origin: {kind: "human"}` |
| Tool result | `message.content` is a list of `tool_result` blocks; the record has `toolUseResult` |
| Interrupt marker | text block starting `[Request interrupted by user` |
| Injected by Claude Code | `isMeta: true` (skill bodies, review targets, caveats) |
| Slash-command wrapper | string starting with `<command-name>`, `<local-command-stdout>`, `<local-command-caveat>`, … |
| Compaction summary | `isCompactSummary: true`; preceded by a `system` record with `subtype: "compact_boundary"` and `compactMetadata` (`trigger`, `preTokens`, `postTokens`) |

Pasted content (JSON, logs) arrives as a normal typed prompt.

### Tools

In the sample, `Bash` is **85%** of tool calls; `Read`/`Edit`/`Write` together are under 6%.
File-touching tools carry `input.file_path` (the anchor for attribution), but most real file
work happens inside Bash commands (heredocs, scripts, `cd … &&`). **Phase 3 attribution must
parse Bash commands for paths**, not only `Edit`/`Write`.

`toolUseResult` holds structured results (`stdout`/`stderr` for Bash; `filePath`,
`structuredPatch` for Edit/Write). Seamline uses only the `tool_result` block text for now.

## Resumed and forked sessions copy history

Resuming a session writes a **new** transcript that starts with a copy of the parent's
records: **same `uuid`, same `timestamp`**, with `sessionId` rewritten to the new id. In the
sample, three sessions of one project formed a resume chain, sharing up to 1,900
records.

- Without de-duplication every fact would be extracted once per resume.
- The minimum timestamp can't order sessions (copies keep old timestamps). The **first line**
  (a `queue-operation` with no `uuid`, never copied) carries the file's creation time, and
  matches the file's birth time on disk. `SessionInfo.started` uses it.
- Rule: a record belongs to the earliest-created session containing its `uuid`; in later
  sessions it is *inherited* and labeled `skip` (`inherited_uuids()` in `discover.py`).

## cwd

- The first recorded `cwd` is the folder the session started in → the default service.
- `cwd` changes mid-session when Claude `cd`s (seen: `app` → `app/app`,
  `project` → `project/reports/curve`). Per-record `cwd` is kept on each event for
  attribution later.

## Subagents

Agent-tool runs are stored separately in `<session>/subagents/agent-*.jsonl` with
`isSidechain: true` and an `agentId`. Their "user" prompts are written by Claude, so they
never become `keep`; their file tool calls can still be anchors. Seamline counts them in
`seamline sessions` but doesn't read them yet.

## Redaction

`redact.py` runs inside the reader, before anything else sees the text: provider keys
(Anthropic, OpenAI, AWS, GitHub, Slack, Google), JWTs, private key blocks, URL credentials,
Bearer/Basic headers, and `NAME=value` / `"name": "value"` where the name contains
SECRET/TOKEN/PASSWORD/_KEY/…. Results of `Read` calls on `.env` files are masked entirely
(only when the call and its result are in the same read; the generic patterns cover the rest).

On the sample: 141 masks in evidence, 4 in keep. Known harmless false positives:
`self.api_key = api_key` (a variable name masked) and phrases like "action token: X".

## Open questions

- Do sessions started from the terminal CLI (`entrypoint` other than `claude-desktop`) differ?
- What does a `/clear` or a fork from an earlier message look like (partial copies)?
- Very long transcripts (the largest sample is 10 MB) are read whole by `show`; the worker
  will read incrementally from a saved byte offset (already supported by `read_events`).
