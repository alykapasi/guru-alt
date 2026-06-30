"""Per-call cost estimation.

Prices are USD per 1M tokens (input, output). Unknown models (e.g. local Ollama)
cost nothing. Model strings may be bare (`claude-sonnet-4-6`) or provider-qualified
(`anthropic/claude-sonnet-4-6`), so we match by substring.
"""

from app.llm.types import Usage

# USD per 1,000,000 tokens: model-key -> (input, output)
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str, usage: Usage) -> float:
    """Estimated cost of a call. Returns 0.0 for models with no known pricing."""
    for key, (price_in, price_out) in _PRICES.items():
        if key in model:
            return (
                usage.input_tokens / 1_000_000 * price_in
                + usage.output_tokens / 1_000_000 * price_out
            )
    return 0.0
