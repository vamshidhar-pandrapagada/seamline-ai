# Extraction eval

Measures how good `seamline extract` is on real sessions. Separate from `tests/`: it calls
the real model and is run by hand.

1. Extract a session and save the result (git-ignored):
   ```bash
   uv run seamline extract <session> --root <project> --json eval/out/<session>.json
   ```
2. Draft a label file from it, then correct it by hand. Delete wrong facts, fix kinds, and
   add facts the extractor missed (quote the transcript; `seamline show <session> --full`
   helps):
   ```bash
   uv run python eval/score_extraction.py draft eval/out/<session>.json > eval/labels/<session>.toml
   ```
3. After changing the prompt or model, re-extract into `eval/out/` and score:
   ```bash
   uv run python eval/score_extraction.py score eval/labels eval/out
   ```

**Gate (Phase 2):** precision ≥ 80% over about 10 labeled sessions. Recall is reported but
not gated: a missed fact costs less than a wrong one injected into another session.

Labels are hand-checked judgments, so draft-then-correct is a starting point, not the
answer: read the session, don't just approve the draft.
