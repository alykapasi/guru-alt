"""Cost accumulation + pricing through the cost-instrumented client."""

import pytest

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatRole, ModelRole
from tests.eval.sweep.cost import CostTrackingClient


def _client(reply: str, model: str, provider: str = "anthropic") -> CostTrackingClient:
    """A cost client whose *provider name* is real even though the backend is the fake.

    Pricing is keyed on the provider a cell is configured to use, so the sweep's own tests
    have to name one — a cell that says "fake" is not a cell anyone pays for.
    """
    inner = LLMClient(
        {provider: FakeProvider(reply=reply)},
        {r: ModelSpec(provider, model) for r in ModelRole},
    )
    return CostTrackingClient(inner)


async def test_accumulates_usage_per_role_and_model() -> None:
    client = _client("one two three", model="claude-opus-4-8")  # 3 output words
    msg = [ChatMessage(role=ChatRole.USER, content="a b")]  # 2 input words
    await client.complete(ModelRole.SMART, msg)
    await client.complete(ModelRole.SMART, msg)

    summary = client.cost_summary()
    assert len(summary.per_role) == 1
    rc = summary.per_role[0]
    assert rc.role == "smart" and rc.model == "claude-opus-4-8"
    assert rc.input_tokens == 4  # 2 calls x 2 input words
    assert rc.output_tokens == 6  # 2 calls x 3 output words
    # opus price = (5.0, 25.0) per 1M tokens
    assert rc.cost_usd == pytest.approx(4 / 1_000_000 * 5.0 + 6 / 1_000_000 * 25.0)
    assert summary.total_tokens == 10
    assert summary.cost_usd == pytest.approx(rc.cost_usd)


async def test_a_cell_running_an_unpriced_model_reports_no_cost_rather_than_zero() -> None:
    """Cheapest-first ranking made 0.0 the winning score, so an unpriced cell would have won."""
    client = _client("hi there", model="some-oss-model", provider="openrouter")  # not in _PRICES
    await client.complete(ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content="q")])
    summary = client.cost_summary()
    assert summary.cost_usd is None
    assert summary.per_role[0].cost_usd is None
    assert summary.total_tokens == 3  # 1 input + 2 output words — tokens are still known


async def test_a_locally_hosted_model_is_free_and_says_so() -> None:
    client = _client("hi there", model="llama3.2", provider="ollama")
    await client.complete(ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content="q")])
    assert client.cost_summary().cost_usd == 0.0
