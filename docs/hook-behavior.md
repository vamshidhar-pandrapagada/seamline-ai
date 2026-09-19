# Hook behavior in the Claude desktop app (Phase 1 experiment)

> Status: **done for Phase 1 (18 Sep 2026).** Headless sessions (`claude -p`, Claude Code 2.1.274) plus one real desktop session (`dfd4715c`). Question 5 moves to Phase 5.

## Questions

1. Do `Stop`, `PreCompact`, `SessionStart`, `UserPromptSubmit` and `SessionEnd` fire in the desktop Code tab?
2. Do user-level hooks fire for sessions started in a service subfolder?
3. What JSON does each hook get on stdin (`session_id`, `transcript_path`, `cwd`, source)?
4. Does output from `SessionStart`/`UserPromptSubmit` actually reach Claude's context?
5. What working directory does an MCP server start in, and can it see the session's `cwd`?

## Evidence already in the transcripts (before the experiment)

- `system` records with `subtype: "stop_hook_summary"` appear after every turn in desktop
  sessions (316 in the sample), listing `hookCount`/`hookInfos`. **Stop hooks do run** in the
  desktop app; the ones seen are the app's own (`"command": "callback"`).
- `attachment` records of type `hook_additional_context` (hookName `PostToolUse:Write`) show
  hook output being added to Claude's context in the desktop app. That points to "yes" for Q4,
  at least for PostToolUse; the experiment checks SessionStart and UserPromptSubmit.
- Claude Code exports `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_ENTRYPOINT` and others to child
  processes (seen from a Bash tool call). If MCP servers get the same variables, Q5's "which
  session is calling?" has an easy answer.

## Running the experiment

The experiment runs in **one project** (`examples/shop`): hooks go into
`examples/shop/.claude/settings.local.json` (git-ignored). `~/.claude/settings.json` and every
other project are untouched.

```bash
uv run seamline init --yes examples/shop                   # if not done: the probe words need seamline.toml
scripts/probe_hooks.sh install --project examples/shop    # adds the logger for 7 events
```

Then in the desktop app:

1. Open a **new** session in `examples/shop`, and another in `examples/shop/services/orders`.
2. In each, ask: *"What are the session probe word and the prompt probe word?"* Expected if
   hook output reaches Claude: `lighthouse` and `harbor`.
3. Chat for a few turns; run `/compact` once; close a session.

```bash
scripts/probe_hooks.sh report                               # calls per event, payload fields, cwd vs $PWD vs CLAUDE_PROJECT_DIR, timing
scripts/probe_hooks.sh uninstall --project examples/shop   # removes the file it created
rm -r ~/.seamline-probe                                     # delete the log when done
```

**What the log contains:** event name, time, session id, transcript path, cwd, `$PWD`,
`$CLAUDE_PROJECT_DIR`, the *names* of `CLAUDE*` environment variables, and timing. **No
conversation content:** every payload field that isn't known metadata is reduced to its shape
(`"prompt": "<42 chars>"`, `"tool_input": "<object: command>"`).

**What the subfolder session tests (question 2):** whether Claude Code finds the project's
`.claude/settings.local.json` when a session starts in a subfolder. If yes, Seamline could
install hooks per project instead of user-wide (and wouldn't need the Phase 4 fast-exit
wrapper). If no, user-level hooks are required; `probe_hooks.sh install` without `--project`
tests that, but it affects every project.

## Results

Headless runs on 18 Sep 2026, with hooks in `examples/shop/.claude/settings.local.json`. The
sessions couldn't authenticate from a clean environment, but hooks fire before the model
call, so events, payloads and settings scope were still observable. No model reply means
question 4 is still open.

| Question | Answer | Evidence |
|---|---|---|
| 1. Events that fire | `SessionStart` (source `startup`), `UserPromptSubmit`, `Stop`, `SessionEnd` all fire, including in the desktop app (SessionStart/UserPromptSubmit/Stop seen in dfd4715c). `PreCompact` and `SubagentStop` not exercised yet | 13 log lines across 4 sessions |
| 2. Subfolder sessions | **Project hooks apply only to sessions started in exactly that folder.** Hooks in `shop/.claude/` did **not** fire for a session in `shop/services/orders` (0 calls, though the session started). Hooks placed in `services/orders/.claude/` **did** fire there | Runs e0ad/842e (root) vs the orders runs |
| 3. stdin payload | Every event: `session_id`, `transcript_path`, `cwd`, `hook_event_name`. Plus `source` (SessionStart); `prompt`, `prompt_id`, `permission_mode` (UserPromptSubmit); `last_assistant_message`, `stop_hook_active` (Stop); `reason` (SessionEnd) | `probe_hooks.sh report` |
| 4. Output reaches Claude | **Yes, in the desktop app.** Asked for the probe words, Claude answered `lighthouse` (SessionStart) and `harbor` (UserPromptSubmit) | Desktop session dfd4715c, 18 Sep |
| 5. MCP cwd / session | Not tested yet (Phase 5) | |

Also observed:
- payload `cwd`, `$PWD` and `$CLAUDE_PROJECT_DIR` were identical in every call, so the Phase 4
  wrapper can rely on the process's working directory without parsing JSON.
- `transcript_path` is given directly, so hooks don't need to guess the encoded folder.
- Each hook call took 14–17 ms (shell + jq + perl timing).

**Design consequence:** hooks can stay **project-scoped** with no user-level install. `seamline
init` writes `.claude/settings.local.json` into the project root **and each service folder**.
Nothing runs in other projects. Trade-off: a session opened in an unlisted subfolder (e.g.
`services/orders/src`) gets no hooks, so no live brief. Its transcript is still found by
cwd-based discovery and ingested the next time the worker runs.

**Revisited (19 Sep 2026):** the per-folder design left every new or unlisted folder
without hooks. `seamline install` now adds the hooks once to the user settings instead, each
wrapped in a `/bin/sh` guard that walks up from `$PWD` to find `seamline.toml` and exits
before Python starts when there is none, so other projects still see no Seamline activity.
The per-folder setup remains the fallback when `install` hasn't been run; a folder with both
handles each event once (the user-level call steps aside). The MCP server stays per project
(`.mcp.json` at the root, found from any subfolder); its approval in the root's
`.claude/settings.local.json` covered sessions in `a service folder and a folder below it in a
project that is its own git repo (`claude mcp list`, 19 Sep).
