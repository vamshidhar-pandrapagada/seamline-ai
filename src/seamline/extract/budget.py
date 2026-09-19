"""Record what every model call costs, and optionally refuse calls past a daily cap.

The cost comes from the token usage the API reports, priced with `pricing.PRICES`. Before a
capped call, a rough estimate (prompt size plus typical output) must still fit under the cap,
so the day's total can overshoot by at most one call's estimation error.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date

from seamline.extract.pricing import (
    CHARS_PER_TOKEN,
    OUTPUT_TOKENS_PER_EXCERPT,
    OUTPUT_TOKENS_PER_VERDICT,
    price_for,
    usd_for,
)
from seamline.extract.providers import RECORD_FACTS, Completion, LLMProvider, ProviderStop, Tool
from seamline.ledger import queries as q


class BudgetExceeded(ProviderStop):
    """The next call would pass the daily cap, or its cost can't be known."""


def estimate_call_usd(model: str, system: str, prompt: str, tool: Tool) -> float | None:
    price = price_for(model)
    if price is None:
        return None
    input_tokens = (len(system) + len(prompt)) / CHARS_PER_TOKEN
    output_tokens = (
        OUTPUT_TOKENS_PER_EXCERPT if tool.name == RECORD_FACTS.name else OUTPUT_TOKENS_PER_VERDICT
    )
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


class MeteredProvider:
    """Wraps a provider: records each call's cost in the ledger; with `cap_usd`, enforces it."""

    def __init__(
        self,
        inner: LLMProvider,
        conn: sqlite3.Connection,
        model: str,
        *,
        cap_usd: float | None = None,
        today: Callable[[], date] = date.today,
    ):
        self.inner = inner
        self.conn = conn
        self.model = model
        self.cap_usd = cap_usd
        self.today = today
        self.spent_usd = 0.0  # This wrapper's calls only
        self.refused: BudgetExceeded | None = None  # Set once a call was refused

    @property
    def name(self) -> str:
        return self.inner.name

    def complete(
        self, system: str, prompt: str, schema: dict, tool: Tool = RECORD_FACTS
    ) -> Completion:
        day = self.today().isoformat()
        if self.cap_usd is not None:
            estimate = estimate_call_usd(self.model, system, prompt, tool)
            if estimate is None:
                self.refused = BudgetExceeded(
                    f"no price known for {self.model}, so the daily cap can't be enforced; "
                    "add it to seamline/extract/pricing.py or pick a listed model"
                )
                raise self.refused
            spent = q.spent_on(self.conn, day)
            if spent + estimate > self.cap_usd:
                self.refused = BudgetExceeded(
                    f"daily cap reached: ${spent:.2f} spent today, the next call would cost "
                    f"about ${estimate:.2f}, cap ${self.cap_usd:.2f}; work resumes tomorrow"
                )
                raise self.refused
        completion = self.inner.complete(system, prompt, schema, tool)
        model = completion.model or self.model
        usd = usd_for(model, completion.input_tokens, completion.output_tokens)
        # Commit at once unless the caller has a transaction open (it commits the row then).
        own = not self.conn.in_transaction
        q.record_spend(
            self.conn, day, model, tool.name, completion.input_tokens, completion.output_tokens, usd
        )
        if own:
            self.conn.commit()
        self.spent_usd += usd or 0.0
        return completion
