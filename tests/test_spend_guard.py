"""Caps hold on every paid call, for concurrent calls too, and background work yields first (S47)."""

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm import ChatMessage, ChatRole, ModelRole
from app.llm.attribution import attributed
from app.llm.meter import BudgetExceeded
from app.llm.providers.fake import FakeProvider
from app.llm.registry import fake_llm_client
from app.models.chat import LLMCall
from app.models.learner import Learner
from app.services import spend_guard

HELLO = [ChatMessage(role=ChatRole.USER, content="hello")]


@pytest.fixture(autouse=True)
def fresh_cache():
    spend_guard.reset_cache()
    yield
    spend_guard.reset_cache()


def _settings(monkeypatch, **kwargs) -> None:
    monkeypatch.setattr(spend_guard, "get_settings", lambda: Settings(**kwargs))


async def _learner(session: AsyncSession) -> uuid.UUID:
    learner = Learner(handle=f"g-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner.id


async def _spent(session: AsyncSession, learner_id: uuid.UUID | None, *, tokens: int, cost=None):
    session.add(
        LLMCall(
            learner_id=learner_id,
            role="smart",
            provider="fake",
            model="fake-1",
            input_tokens=tokens,
            output_tokens=0,
            cost_usd=cost,
            feature="chat_turn",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await session.commit()


async def test_a_learner_over_the_cap_is_refused_before_any_provider_call(
    db_session: AsyncSession, monkeypatch
) -> None:
    _settings(monkeypatch, learner_daily_token_limit=1000)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=1000)
    llm = fake_llm_client("unreached")

    with (
        attributed(learner_id=learner_id, feature="chat_turn"),
        pytest.raises(BudgetExceeded) as exc,
    ):
        await llm.complete(ModelRole.FAST, HELLO)

    assert exc.value.scope == "learner"
    assert cast(FakeProvider, llm._providers["fake"]).prompts_sent == []


async def test_a_reservation_counts_before_its_call_returns(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Review focus 1: an open stream holds a reservation the next call sees."""
    _settings(monkeypatch, learner_daily_token_limit=3000)
    learner_id = await _learner(db_session)
    llm = fake_llm_client("one two three")
    with attributed(learner_id=learner_id, feature="chat_turn"):
        stream = llm.stream(ModelRole.SMART, HELLO, max_tokens=2000)
        await anext(stream)  # the pending row exists; the stream has not finished
        with pytest.raises(BudgetExceeded):
            await llm.complete(ModelRole.FAST, HELLO, max_tokens=2000)
        await stream.aclose()


async def test_background_work_stops_at_ninety_percent(
    db_session: AsyncSession, monkeypatch
) -> None:
    _settings(monkeypatch, learner_daily_token_limit=10_000)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=9_000)
    llm = fake_llm_client("ok")

    with attributed(learner_id=learner_id, feature="profile_refresh", background=True):
        with pytest.raises(BudgetExceeded):
            await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)
    with attributed(learner_id=learner_id, feature="chat_turn"):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)  # live work still allowed


async def test_the_deployment_ceiling(db_session: AsyncSession, monkeypatch) -> None:
    _settings(monkeypatch, spend_budget_usd=10.0)
    await _spent(db_session, None, tokens=1, cost=9.5)
    llm = fake_llm_client("ok")

    with attributed(background=True, feature="reindex"), pytest.raises(BudgetExceeded) as exc:
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)
    assert exc.value.scope == "deployment"
    assert await spend_guard.background_paused() is True

    await _spent(db_session, None, tokens=1, cost=1.0)
    spend_guard.reset_cache()
    with attributed(feature="chat_turn"), pytest.raises(BudgetExceeded):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)


async def test_the_deployment_total_is_cached(db_session: AsyncSession, monkeypatch) -> None:
    _settings(monkeypatch, spend_budget_usd=10.0, spend_guard_cache_seconds=3600)
    llm = fake_llm_client("ok")
    with attributed(feature="chat_turn"):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)  # caches a small total
    await _spent(db_session, None, tokens=1, cost=50.0)
    with attributed(feature="chat_turn"):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)  # still the cached total


async def test_a_pending_row_counts_at_its_estimate(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    db_session.add(
        LLMCall(
            learner_id=learner_id,
            role="smart",
            provider="fake",
            model="fake-1",
            input_tokens=10,
            output_tokens=990,
            status="pending",
            estimated=True,
            feature="chat_turn",
        )
    )
    await db_session.commit()
    assert (await spend_guard.spend_since(db_session, learner_id)).tokens == 1000


async def test_the_preflight_refuses_a_learner_already_at_the_cap(
    db_session: AsyncSession, monkeypatch
) -> None:
    """With nothing reserved yet, exactly at the cap is already spent."""
    _settings(monkeypatch, learner_daily_token_limit=1000)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=1000)
    with pytest.raises(BudgetExceeded):
        await spend_guard.check(db_session, learner_id)
