"""Per-call cost estimation.

Prices are USD per 1M tokens (input, output). Model strings may be bare
(`claude-sonnet-4-6`) or provider-qualified (`anthropic/claude-sonnet-4-6`), so we match
by substring.

A model we have no price for is **unknown, not free**. Returning 0.0 for it made an
unpriced production model indistinguishable from a locally hosted one, so a spend total
that was really "we don't know" read as "nothing". Only providers we host ourselves are
genuinely zero.
"""

import structlog

from app.llm.types import Usage

log = structlog.get_logger(__name__)

# Providers with no per-token charge: the model runs on hardware we already pay for.
LOCAL_PROVIDERS = frozenset({"ollama", "fake"})

# USD per 1,000,000 tokens: model-key -> (input, output). Embedding models bill input only.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}

_warned: set[str] = set()


def _warn_once(provider: str, model: str) -> None:
    """One warning per unpriced model, not one per call — this fires on every request."""
    key = f"{provider}:{model}"
    if key not in _warned:
        _warned.add(key)
        log.warning("pricing.unknown_model", provider=provider, model=model)


def price_usd(provider: str, model: str, usage: Usage) -> float | None:
    """Estimated cost of a call, or ``None`` when this model has no known price.

    ``None`` and ``0.0`` mean different things and must not be collapsed: 0.0 is "this ran
    on our own hardware", ``None`` is "this cost something we cannot name".
    """
    if provider in LOCAL_PROVIDERS:
        return 0.0
    for key, (price_in, price_out) in _PRICES.items():
        if key in model:
            return (
                usage.input_tokens / 1_000_000 * price_in
                + usage.output_tokens / 1_000_000 * price_out
            )
    _warn_once(provider, model)
    return None
