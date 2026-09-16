"""Concrete LLM provider backends."""

from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.providers.shaped import ShapedProvider

DETERMINISTIC_PROVIDERS = frozenset({FakeProvider.name, ShapedProvider.name})
"""Providers that answer without a model.

`app.core.release` refuses to start production with any role routed to one of these: every
request would succeed and every answer would be invented, which no health check or readiness
probe can tell apart from working. `tests/test_release.py` walks the registry's real provider
table and fails if a provider marked ``deterministic`` is missing from this set, so adding a
third stand-in cannot quietly skip the guard.
"""

__all__ = [
    "DETERMINISTIC_PROVIDERS",
    "AnthropicProvider",
    "FakeProvider",
    "FakeTurn",
    "OpenAICompatProvider",
    "ShapedProvider",
]
