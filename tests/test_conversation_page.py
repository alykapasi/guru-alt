"""The conversation list is a page, not the whole history (S62)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation
from app.models.learner import Learner
from app.services import chat as chat_svc
from tests.history import SMALL, seed_history
from tests.querycount import count_queries

API = "/api/v1"


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"cp-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_the_list_does_not_grow_with_history(db_session: AsyncSession) -> None:
    small = await seed_history(db_session, (await _learner(db_session)).id, SMALL)
    large = await seed_history(
        db_session, (await _learner(db_session)).id, replace(SMALL, conversations=60)
    )
    with count_queries(db_session) as s:
        await chat_svc.list_conversations(db_session, small.learner_id, limit=5)
    with count_queries(db_session) as big:
        await chat_svc.list_conversations(db_session, large.learner_id, limit=5)
    assert len(big) <= len(s) + 2, big
    assert big.rows <= s.rows + 2, big


async def test_pages_walk_back_without_gaps_or_repeats(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    h = await seed_history(
        db_session, learner.id, replace(SMALL, conversations=15)
    )  # 15 + practice
    seen: list[uuid.UUID] = []
    before = None
    while True:
        page, has_more = await chat_svc.list_conversations(
            db_session, learner.id, limit=4, before=before
        )
        seen += [c.id for c in page]
        if not has_more:
            break
        before = page[-1].id
    assert len(seen) == len(set(seen)) == 16
    assert h.conversation_id in seen


async def test_ties_at_a_page_boundary_are_neither_skipped_nor_repeated(
    db_session: AsyncSession,
) -> None:
    """``created_at`` is the transaction's clock: conversations made together share it."""
    learner = await _learner(db_session)
    stamp = datetime.now(UTC).replace(tzinfo=None)
    for _ in range(5):
        db_session.add(Conversation(learner_id=learner.id, created_at=stamp))
    await db_session.flush()
    first, more = await chat_svc.list_conversations(db_session, learner.id, limit=3)
    second, done = await chat_svc.list_conversations(
        db_session, learner.id, limit=3, before=first[-1].id
    )
    assert more and not done
    assert len({c.id for c in first + second}) == 5


async def test_a_foreign_cursor_yields_the_first_page(db_session: AsyncSession) -> None:
    mine = await _learner(db_session)
    theirs = await _learner(db_session)
    await seed_history(db_session, mine.id, SMALL)
    other = Conversation(learner_id=theirs.id)
    db_session.add(other)
    await db_session.flush()
    page, _ = await chat_svc.list_conversations(db_session, mine.id, limit=3, before=other.id)
    first, _ = await chat_svc.list_conversations(db_session, mine.id, limit=3)
    assert [c.id for c in page] == [c.id for c in first]
    assert all(c.learner_id == mine.id for c in page)


async def test_the_endpoint_returns_a_bounded_page(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    await seed_history(
        db_session, api_learner.id, replace(SMALL, conversations=60)
    )  # 60 + practice
    body = (await api_client.get(f"{API}/conversations")).json()
    assert len(body["conversations"]) == 50
    assert body["has_more"] is True
    clamped = (await api_client.get(f"{API}/conversations", params={"limit": 10_000})).json()
    assert len(clamped["conversations"]) == 61  # under the 200 ceiling: everything
    assert clamped["has_more"] is False


async def test_a_conversation_is_readable_by_id(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The chat page used to find its conversation in the list; an old one is off the page."""
    h = await seed_history(db_session, api_learner.id, replace(SMALL, conversations=60))
    oldest = (await chat_svc.list_conversations(db_session, api_learner.id, limit=200))[0][-1]
    r = await api_client.get(f"{API}/conversations/{oldest.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(oldest.id)
    assert (await api_client.get(f"{API}/conversations/{uuid.uuid4()}")).status_code == 404
    assert h.learner_id == api_learner.id
