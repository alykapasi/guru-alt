"""Caps hold on every paid call, for concurrent calls too, and background work yields first (S47)."""

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import func, select
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


async def test_a_refused_route_answers_429_with_its_scope(
    api_client, db_session: AsyncSession, api_learner: Learner, monkeypatch
) -> None:
    _settings(monkeypatch, learner_daily_token_limit=10)
    await _spent(db_session, api_learner.id, tokens=10)
    conv = (await api_client.post("/api/v1/conversations", json={})).json()

    r = await api_client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}
    )

    assert r.status_code == 429
    assert r.json()["detail"] == {
        "code": "budget_exceeded",
        "scope": "learner",
        "message": "You've reached today's usage limit. It resets over the next 24 hours.",
    }


async def test_a_turn_refused_mid_way_says_why(db_session: AsyncSession, monkeypatch) -> None:
    """Review focus 3: the learner sees the budget message, not "generation failed"."""
    from app.models.chat import Conversation
    from app.services import chat as chat_svc

    _settings(monkeypatch, learner_daily_token_limit=10)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=10)
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.commit()

    events = [
        e
        async for e in chat_svc.run_tutor_turn(
            db_session,
            fake_llm_client("unreached"),
            learner_id=learner_id,
            conversation=conversation,
            history=[],
            user_content="hello",
            max_tokens=200,
            source_ids=[],
        )
    ]

    errors = [e for e in events if e.type == "error"]
    assert errors and errors[0].detail.startswith("You've reached today's usage limit")


async def test_background_work_refused_is_deferred_quietly(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Review focus 4."""
    import contextlib

    from app.models.chat import Conversation, Message
    from app.workers import tasks

    _settings(monkeypatch, learner_daily_token_limit=100)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=95)

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tasks, "SessionFactory", factory)
    monkeypatch.setattr(
        tasks, "build_llm_client", lambda _s: fake_llm_client('{"interests": ["chess"]}')
    )
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.flush()
    # A message, so the interests estimator would make a model call.
    db_session.add(Message(conversation_id=conversation.id, role="user", content="I love chess."))
    await db_session.commit()

    # Recorded from the logger itself: another test's logging setup can stop propagation.
    logged: list[str] = []
    monkeypatch.setattr(tasks.logger, "info", lambda msg, *a: logged.append(msg % a))

    await tasks._profile_refresh_task(str(learner_id))  # must not raise
    assert logged == ["budget.deferred task=profile_refresh"]


async def test_an_ingestion_refused_carries_the_message_to_the_learner(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The source fails with a reason the learner can read, and can retry tomorrow."""
    from app.models.source import SourceKind
    from app.services import ingestion
    from app.storage import InMemoryBlobStore

    _settings(monkeypatch, learner_daily_token_limit=10)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=10)
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner_id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=b"Photosynthesis turns light into chemical energy in the chloroplast.",
    )
    await db_session.commit()

    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert result is not None
    assert result.error == "You've reached today's usage limit. It resets over the next 24 hours."


async def test_a_reply_refused_at_generation_says_why(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The turn's own error handler around generation must not turn a refusal into
    "generation failed"."""
    from app.models.chat import Conversation, LLMCall
    from app.services import chat as chat_svc

    _settings(monkeypatch, learner_daily_token_limit=1500)
    learner_id = await _learner(db_session)
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.commit()

    events = [
        e
        async for e in chat_svc.run_tutor_turn(
            db_session,
            fake_llm_client("unreached"),
            learner_id=learner_id,
            conversation=conversation,
            history=[],
            user_content="hello",
            max_tokens=2000,  # the reply's reservation alone is over the cap
            source_ids=[],
        )
    ]

    errors = [e for e in events if e.type == "error"]
    assert errors and errors[0].detail.startswith("You've reached today's usage limit")
    smart = await db_session.scalar(select(func.count()).where(LLMCall.role == "smart"))
    assert smart == 0  # refused before any row or provider call


async def test_the_sweep_claims_nothing_past_ninety_percent(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Claiming would stamp items attempted; paused, everything due simply stays due."""
    from app.services import refresh_schedule
    from app.workers import tasks

    _settings(monkeypatch, spend_budget_usd=10.0)
    await _spent(db_session, None, tokens=1, cost=9.5)
    claims: list[object] = []

    async def claim_due(*args, **kwargs):
        claims.append(kwargs)
        return refresh_schedule.Claimed(conversations=[], learners=[])

    monkeypatch.setattr(refresh_schedule, "claim_due", claim_due)
    await tasks._refresh_due_once()
    assert claims == []


class _Refusing(FakeProvider):
    async def complete(self, **kwargs):
        raise BudgetExceeded("learner")


def _refusing_client():
    from app.llm import LLMClient
    from app.llm.registry import ModelSpec

    return LLMClient({"fake": _Refusing()}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def test_judges_pass_a_refusal_through_rather_than_guessing() -> None:
    """A refused judge is not an undecided or a "coexists" verdict: the work is deferred."""
    from app.learning import link_judge
    from app.memory import supersession

    side = link_judge.Side(kc_name="a", description=None, topic_name="t", subject_name="s")
    with pytest.raises(BudgetExceeded):
        await link_judge.judge_pair(_refusing_client(), side, side)
    with pytest.raises(BudgetExceeded):
        await supersession.judge(_refusing_client(), [supersession.Candidate("x", ["y"])])
