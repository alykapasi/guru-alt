"""Offline unit tests for the LLM layer (pricing, registry, FakeProvider)."""

import pytest
from structlog.testing import capture_logs

from app.llm import pricing
from app.llm.pricing import price_usd
from app.llm.registry import _parse_spec, fake_llm_client
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage


def test_pricing_known_and_unknown() -> None:
    one_million_each = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price_usd("anthropic", "claude-sonnet-4-6", one_million_each) == pytest.approx(18.0)
    # provider-qualified names match by substring
    assert price_usd("openrouter", "anthropic/claude-haiku-4-5", one_million_each) == pytest.approx(
        6.0
    )


def test_a_model_we_cannot_price_is_unknown_not_free() -> None:
    """The whole point: an unpriced paid model must not be reported as costing nothing."""
    one_million_each = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    # Locally hosted: genuinely free, and we can say so.
    assert price_usd("ollama", "llama3.2", one_million_each) == 0.0
    # A paid provider serving a model with no price entry: unknown, and 0.0 would be a lie.
    assert price_usd("openrouter", "some/brand-new-model", one_million_each) is None


def test_an_unpriced_model_is_reported_once_not_once_per_call() -> None:
    """This fires on every request; a per-call warning would bury the log it belongs in."""
    usage = Usage(input_tokens=10, output_tokens=10)
    pricing._warned.discard("openrouter:noisy/model")
    with capture_logs() as logs:
        for _ in range(3):
            price_usd("openrouter", "noisy/model", usage)
    assert [log["event"] for log in logs] == ["pricing.unknown_model"]


def test_parse_spec() -> None:
    spec = _parse_spec("ollama:llama3.2")
    assert spec.provider == "ollama"
    assert spec.model == "llama3.2"
    with pytest.raises(ValueError):
        _parse_spec("no-colon")


def test_registry_resolves_every_role_to_fake() -> None:
    client = fake_llm_client()
    for role in ModelRole:
        assert client.spec(role).provider == "fake"


async def test_fake_client_streams_text_and_usage() -> None:
    client = fake_llm_client("hi there friend")
    chunks = [
        chunk
        async for chunk in client.stream(
            ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content="x")]
        )
    ]
    text = "".join(c.text for c in chunks if c.text)
    usage = next((c.usage for c in chunks if c.usage is not None), None)
    assert text == "hi there friend"
    assert usage is not None
    assert usage.output_tokens == 3
