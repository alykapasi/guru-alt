"""What a learner may send, carry, and spend before a turn is refused.

Output caps and tool-iteration limits already existed; nothing bounded accumulation. These
cover the three bounds that were missing: message size, the history a turn carries, and a
learner's running total.
"""

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_llm_client
from app.core.config import Settings, get_settings
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.schemas.chat import ChatTurnRequest
from app.services import budget
from app.services import chat as chat_svc

API = "/api/v1"
REPLY = "Let us explore this together."


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def _conversation(session: AsyncSession, learner: Learner, *, messages: int = 0):
    conversation = Conversation(learner_id=learner.id, title="t", goal="learn algebra")
    session.add(conversation)
    await session.flush()
    # Explicit timestamps: created_at defaults to *transaction-start* time, so messages
    # written together would all carry the same one. In production each turn commits
    # separately and they differ, which is what this reproduces.
    base = datetime(2026, 1, 1)
    for i in range(messages):
        session.add(
            Message(
                conversation_id=conversation.id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"message {i}",
                created_at=base + timedelta(minutes=i),
            )
        )
    await session.flush()
    return conversation


async def _learner(session: AsyncSession) -> Learner:
    """A learner who is not the one the API client is signed in as."""
    learner = Learner(handle=f"b-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


# --- message size ----------------------------------------------------------------------------


def test_the_schema_bound_matches_the_configured_one() -> None:
    """Pydantic constraints are class-level, so the literal and the setting drift silently."""
    bound = ChatTurnRequest.model_fields["content"].metadata
    assert any(getattr(m, "max_length", None) == get_settings().chat_max_input_chars for m in bound)


async def test_an_oversized_message_is_refused_before_anything_is_paid_for(
    db_session: AsyncSession, api_client: AsyncClient, fake_llm: None, api_learner: Learner
) -> None:
    learner = api_learner
    conversation = await _conversation(db_session, learner)
    await db_session.commit()

    response = await api_client.post(
        f"{API}/conversations/{conversation.id}/messages",
        json={"content": "x" * (get_settings().chat_max_input_chars + 1)},
    )

    assert response.status_code == 422
    assert (await db_session.scalars(select(LLMCall))).all() == []


# --- history window --------------------------------------------------------------------------


async def test_the_turn_window_and_the_transcript_page_are_different_bounds(
    db_session: AsyncSession,
) -> None:
    """Both reads are bounded now (S62), and they are bounded for different reasons.

    The turn window is what gets *sent to a model*, so it is small and priced in tokens. The
    transcript page is what the client *renders*, so it is large and priced in query cost.
    Collapsing them would either make a turn ruinously expensive or make the transcript
    useless to read.
    """
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, messages=100)

    page, has_more = await chat_svc.list_messages(db_session, conversation.id, limit=60)
    window = await chat_svc.recent_messages(db_session, conversation.id, limit=10)

    assert len(page) == 60 and has_more is True
    assert [m.content for m in window] == [f"message {i}" for i in range(90, 100)]
    # Both take their window from the newest end, so the turn's history is a suffix of the
    # page the learner is looking at rather than a different slice of the conversation.
    assert [m.content for m in page[-10:]] == [m.content for m in window]


async def test_the_window_is_the_most_recent_messages_in_order(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, messages=3)
    window = await chat_svc.recent_messages(db_session, conversation.id, limit=10)
    assert [m.content for m in window] == ["message 0", "message 1", "message 2"]


# --- learner spend ---------------------------------------------------------------------------


async def _record(session: AsyncSession, learner: Learner, *, cost, tokens: int) -> None:
    session.add(
        LLMCall(
            learner_id=learner.id,
            role="smart",
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=tokens,
            output_tokens=0,
            cost_usd=cost,
        )
    )
    await session.flush()


async def test_spend_sums_what_the_accounting_log_recorded(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _record(db_session, learner, cost=0.25, tokens=1_000)
    await _record(db_session, learner, cost=0.75, tokens=2_000)

    spend = await budget.spend_since(db_session, learner.id)
    assert spend.cost_usd == pytest.approx(1.0)
    assert spend.tokens == 3_000


async def test_an_unpriced_call_still_counts_against_the_token_ceiling(
    db_session: AsyncSession,
) -> None:
    """A cost-only budget would be silently unenforced for exactly the models we cannot price."""
    learner = await _learner(db_session)
    await _record(db_session, learner, cost=None, tokens=9_000)

    spend = await budget.spend_since(db_session, learner.id)
    assert spend.cost_usd == 0.0  # nothing known to add
    assert spend.tokens == 9_000

    settings = Settings(learner_daily_cost_usd_limit=100.0, learner_daily_token_limit=5_000)
    with pytest.raises(budget.BudgetExceeded, match="token limit"):
        await budget.require_budget(db_session, learner.id, settings)


async def test_spend_outside_the_window_does_not_count(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _record(db_session, learner, cost=5.0, tokens=1_000)

    recent = await budget.spend_since(db_session, learner.id, window=timedelta(hours=24))
    ancient = await budget.spend_since(db_session, learner.id, window=timedelta(seconds=0))
    assert recent.tokens == 1_000
    assert ancient.tokens == 0


async def test_a_learner_over_the_ceiling_is_refused_the_turn(
    db_session: AsyncSession, api_client: AsyncClient, fake_llm: None, api_learner: Learner
) -> None:
    learner = api_learner
    conversation = await _conversation(db_session, learner)
    await _record(db_session, learner, cost=99.0, tokens=10)
    await db_session.commit()

    app.dependency_overrides[get_app_settings] = lambda: Settings(learner_daily_cost_usd_limit=1.0)
    try:
        response = await api_client.post(
            f"{API}/conversations/{conversation.id}/messages", json={"content": "hello"}
        )
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert response.status_code == 429
    assert "spend limit" in response.json()["detail"]


async def test_a_learner_under_the_ceiling_is_allowed_through(
    db_session: AsyncSession, api_client: AsyncClient, fake_llm: None, api_learner: Learner
) -> None:
    learner = api_learner
    conversation = await _conversation(db_session, learner)
    await _record(db_session, learner, cost=0.01, tokens=10)
    await db_session.commit()

    response = await api_client.post(
        f"{API}/conversations/{conversation.id}/messages", json={"content": "hello"}
    )
    assert response.status_code == 200


# --- the transcript read is bounded too (S62) ---------------------------------


async def test_the_newest_page_is_returned_by_default(db_session: AsyncSession) -> None:
    """Default means newest, not oldest. A chat opens at what was just said."""
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, messages=30)

    page, has_more = await chat_svc.list_messages(db_session, conversation.id, limit=10)

    assert [m.content for m in page] == [f"message {i}" for i in range(20, 30)]
    assert has_more is True


async def test_a_conversation_shorter_than_a_page_says_there_is_no_more(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, messages=4)

    page, has_more = await chat_svc.list_messages(db_session, conversation.id, limit=10)

    assert len(page) == 4
    assert has_more is False


async def test_a_conversation_exactly_one_page_long_says_there_is_no_more(
    db_session: AsyncSession,
) -> None:
    """The off-by-one that would put a dead 'load earlier' control on a full first page."""
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, messages=10)

    _, has_more = await chat_svc.list_messages(db_session, conversation.id, limit=10)

    assert has_more is False


async def test_before_walks_backwards_without_repeating_or_skipping(
    db_session: AsyncSession,
) -> None:
    """Paging the whole transcript must reconstruct it exactly — every message once."""
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, messages=25)

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page, has_more = await chat_svc.list_messages(
            db_session, conversation.id, limit=10, before=cursor
        )
        seen = [m.content for m in page] + seen
        if not has_more:
            break
        cursor = page[0].id

    assert seen == [f"message {i}" for i in range(25)]


async def test_messages_sharing_a_timestamp_are_not_skipped_by_the_cursor(
    db_session: AsyncSession,
) -> None:
    """`created_at` is transaction-start time, so a whole turn's messages tie on it. A cursor
    keyed on the timestamp alone would step over the rest of the tied group — this is the
    reason the cursor is `(created_at, id)`."""
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner)
    tied = datetime(2026, 1, 1)
    for i in range(6):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content=f"tied {i}",
                created_at=tied,
            )
        )
    await db_session.flush()

    first, has_more = await chat_svc.list_messages(db_session, conversation.id, limit=3)
    assert has_more is True
    older, _ = await chat_svc.list_messages(
        db_session, conversation.id, limit=3, before=first[0].id
    )

    assert len(older) == 3
    assert {m.content for m in older} | {m.content for m in first} == {
        f"tied {i}" for i in range(6)
    }


async def test_an_unknown_cursor_falls_back_to_the_newest_page(
    db_session: AsyncSession,
) -> None:
    """A client holding a stale id wants a transcript, not an error screen."""
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, messages=5)

    page, _ = await chat_svc.list_messages(
        db_session, conversation.id, limit=10, before=uuid.uuid4()
    )

    assert [m.content for m in page] == [f"message {i}" for i in range(5)]


async def test_a_cursor_from_another_conversation_is_ignored(
    db_session: AsyncSession,
) -> None:
    """Otherwise one conversation's cursor silently truncates another's transcript."""
    learner = await _learner(db_session)
    mine = await _conversation(db_session, learner, messages=5)
    theirs = await _conversation(db_session, learner, messages=5)
    foreign, _ = await chat_svc.list_messages(db_session, theirs.id, limit=10)

    page, _ = await chat_svc.list_messages(db_session, mine.id, limit=10, before=foreign[0].id)

    assert len(page) == 5


async def test_the_endpoint_bounds_the_page_without_being_asked(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The wiring test. The bound has to be the endpoint's default, not something a caller
    opts into — a client that never passes `limit` is the one this item exists for."""
    conversation = await _conversation(db_session, api_learner, messages=12)
    await db_session.commit()

    settings = get_settings().model_copy(update={"chat_transcript_page_size": 5})
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        r = await api_client.get(f"{API}/conversations/{conversation.id}/messages")
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["messages"]) == 5
    assert body["has_more"] is True
    assert [m["content"] for m in body["messages"]] == [f"message {i}" for i in range(7, 12)]


async def test_the_endpoint_pages_backwards_with_before(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    conversation = await _conversation(db_session, api_learner, messages=12)
    await db_session.commit()

    first = await api_client.get(
        f"{API}/conversations/{conversation.id}/messages", params={"limit": 5}
    )
    cursor = first.json()["messages"][0]["id"]
    older = await api_client.get(
        f"{API}/conversations/{conversation.id}/messages",
        params={"limit": 5, "before": cursor},
    )

    assert older.status_code == 200, older.text
    assert [m["content"] for m in older.json()["messages"]] == [f"message {i}" for i in range(2, 7)]


async def test_an_oversized_limit_is_clamped_rather_than_refused(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """A client asking for more than the ceiling wants as much transcript as it can have.
    A 422 there would be a worse answer than the ceiling."""
    conversation = await _conversation(db_session, api_learner, messages=12)
    await db_session.commit()

    settings = get_settings().model_copy(update={"chat_transcript_page_max": 4})
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        r = await api_client.get(
            f"{API}/conversations/{conversation.id}/messages", params={"limit": 1000}
        )
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert r.status_code == 200, r.text
    assert len(r.json()["messages"]) == 4
