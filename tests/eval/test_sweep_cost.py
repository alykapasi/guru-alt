"""Cost accumulation + pricing through the cost-instrumented client."""

import pytest

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatRole, ModelRole
from tests.eval.sweep.cost import CostTrackingClient


def _client(reply: str, model: str) -> CostTrackingClient:
    inner = LLMClient(
        {"fake": FakeProvider(reply=reply)}, {r: ModelSpec("fake", model) for r in ModelRole}
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


async def test_unknown_model_prices_to_zero() -> None:
    client = _client("hi there", model="some-oss-model")  # not in _PRICES
    await client.complete(ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content="q")])
    summary = client.cost_summary()
    assert summary.cost_usd == 0.0
    assert summary.total_tokens == 3  # 1 input + 2 output words
