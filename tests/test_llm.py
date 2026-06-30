"""Offline unit tests for the LLM layer (pricing, registry, FakeProvider)."""

import pytest

from app.llm.pricing import cost_usd
from app.llm.registry import _parse_spec, fake_llm_client
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage


def test_pricing_known_and_unknown() -> None:
    one_million_each = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("claude-sonnet-4-6", one_million_each) == pytest.approx(3.0 + 15.0)
    # provider-qualified names match by substring
    assert cost_usd("anthropic/claude-haiku-4-5", one_million_each) == pytest.approx(1.0 + 5.0)
    # unknown / local models cost nothing
    assert cost_usd("llama3.2", one_million_each) == 0.0


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
