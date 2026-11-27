<!-- swerepo:begin -->
@AGENTS.md
<!-- swerepo:end -->

<!-- seamspec:begin (managed by `seamspec init`; edit outside these markers) -->
# SeamSpec Protocol

This repository uses SeamSpec. Locked invariants are enforced by hooks and git pre-commit (and by
CI where the SeamSpec workflow is installed): failing them blocks you from finishing and blocks commits.

1. Before editing a file, call `get_constraints(files)` (or run `seamspec context <files>`).
   Respect every LOCKED invariant. Approaches listed as previously failed (dead ends) failed for
   the stated reason; don't reuse them unless that reason no longer applies.
2. When the user states an agreement or a rule the code must keep, or you establish a behavior
   worth protecting, capture it right away: call `propose_invariant` with a focused,
   behavior-level test (not implementation details). If its code doesn't exist yet, say so and
   propose it once the code works. The tool does not run the test: run
   `seamspec verify --proposed <id>` with your shell tool and fix the test until it passes.
   If the project keeps a list of rules it intends to lock (for example a section of its
   plan), propose each one when its code exists and works.
3. When an approach fails, or the user tells you about one that failed before, call
   `log_dead_end` with what was tried and why it failed. When you and the user settle a design
   choice that shapes future work on specific files (for example deferring a feature after
   measuring it, choosing an algorithm or a data representation, or setting a module boundary),
   and it is a rationale or trade-off rather than something a test can check or a failed attempt,
   call `propose_decision` scoped to those files, so `get_constraints` shows it in later sessions.
4. If your change must intentionally break a LOCKED invariant, do not edit or delete its test.
   Call `request_supersede(invariant_id, reason, replacement_id)` and tell the user.
5. Never edit files under `.seamspec/` directly; use SeamSpec tools. If they are missing or fail,
   use the matching `seamspec` command in your shell (`seamspec --help`). Never search outside
   this project for SeamSpec's settings; if neither works, stop and tell the user.
6. BRAINDUMP.md holds the user's own notes. Never edit it. When asked to sort it, follow
   `/seamspec:drain`. When SeamSpec reports new notes at the start of a session, ask the user once
   whether to sort them; don't sort without a yes, and don't ask again this session after a no.
7. Only humans approve invariants. When proposals are pending, offer an explanation from
   SeamSpec's reviewer (`/seamspec:explain`), and offer to open `seamspec review` for the user in a
   terminal they type into (never in your own shell; never answer its prompts), or remind them to
   run it.
8. When the user runs a `/seamspec:*` workflow command, follow its steps exactly and end with
   the SeamSpec summary block.
9. Users don't have to type commands. Treat plain requests as the matching workflow command:
   "sort my braindump" → drain; "protect this" / "lock that" / an agreement the user states → lock;
   "that didn't work" / a failure the user describes → dead-end;
   a changed requirement for a LOCKED rule → supersede; "what applies here?" → context;
   "wrap up" → checkpoint.
<!-- seamspec:end -->
