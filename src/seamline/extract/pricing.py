"""Model prices: cost estimates before calls, and actual cost from reported token usage."""

from __future__ import annotations

# USD per million tokens (input, output), first-party API list prices.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
CHARS_PER_TOKEN = 4
SYSTEM_PROMPT_TOKENS = 1_800  # The extraction prompt plus the project header, per call
OUTPUT_TOKENS_PER_EXCERPT = 1_500  # Measured ~1k (Opus 5) to ~3k (Haiku 4.5)
OUTPUT_TOKENS_PER_VERDICT = 300


def price_for(model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens; dated IDs match their alias."""
    if model in PRICES:
        return PRICES[model]
    for alias, price in PRICES.items():
        if model.startswith(alias + "-"):
            return price
    return None


def usd_for(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Cost of one call from its reported usage, or None for an unknown model."""
    price = price_for(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def estimate_usd(model: str, excerpt_chars: int, excerpts: int) -> float | None:
    """Estimated cost of extracting these excerpts, or None for an unknown model."""
    price = price_for(model)
    if price is None or excerpts == 0:
        return None if price is None else 0.0
    input_tokens = excerpt_chars / CHARS_PER_TOKEN + SYSTEM_PROMPT_TOKENS * excerpts
    output_tokens = OUTPUT_TOKENS_PER_EXCERPT * excerpts
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
