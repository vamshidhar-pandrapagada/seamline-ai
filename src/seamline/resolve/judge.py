"""Ask the model whether a newer fact replaces an older one, for pairs rules can't decide.

Rules settle the clear cases (a full restatement, a near-identical claim). The judge sees
only the ambiguous ones, which are rare, so the extra cost is a small Haiku call now and
then. Any failure returns None and the caller falls back to rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from seamline.extract.providers import LLMProvider, ProviderError, Tool

JUDGE_TOOL = Tool("record_verdict", "Record whether the newer statement replaces the older one.")

JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "replaces": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["replaces", "reason"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """\
You compare two facts that one service's Claude Code sessions recorded about the same
subject, at different times. Decide whether the NEWER fact replaces the OLDER one.

replaces = true when the older fact is no longer true once the newer one is: a field was
renamed or retyped, a decision was reversed or changed, a behavior was replaced.

replaces = false when both can be true at once: they describe different fields or
different aspects, the newer one only adds detail, or they are about different things
that happen to share words (created_at and updated_at are different fields).

If unsure, answer false: keeping both is safer than dropping a fact that still holds.
Give a one-sentence reason."""


@dataclass(frozen=True)
class Verdict:
    replaces: bool
    reason: str


class Judge(Protocol):
    def replaces(self, newer: dict, older: dict) -> Verdict | None: ...


class LLMJudge:
    def __init__(self, provider: LLMProvider):
        self.provider = provider
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def replaces(self, newer: dict, older: dict) -> Verdict | None:
        prompt = (
            f"OLDER fact:\n{json.dumps(older, indent=2)}\n\n"
            f"NEWER fact:\n{json.dumps(newer, indent=2)}"
        )
        try:
            out = self.provider.complete(JUDGE_SYSTEM, prompt, JUDGE_SCHEMA, JUDGE_TOOL)
        except ProviderError:
            return None
        self.calls += 1
        self.input_tokens += out.input_tokens
        self.output_tokens += out.output_tokens
        if not isinstance(out.data.get("replaces"), bool):
            return None
        return Verdict(out.data["replaces"], str(out.data.get("reason", "")))


def describe(
    kind: str, service: str | None, interface: str | None, claim: str, details: list[dict]
) -> dict:
    """The view of a fact the judge sees: no ids, no quotes, just what it says."""
    return {
        "kind": kind,
        "service": service or "integration",
        "interface": interface,
        "claim": claim,
        "details": {d["name"]: d["value"] for d in details},
    }
