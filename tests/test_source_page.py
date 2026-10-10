"""The source list is a page, not the whole library (S62)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.models.source import Source
from app.services import sources as sources_svc
from tests.history import SMALL, seed_history
from tests.querycount import count_queries

API = "/api/v1"


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"sp-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_the_list_does_not_grow_with_history(db_session: AsyncSession) -> None:
    small = await seed_history(db_session, (await _learner(db_session)).id, SMALL)
    large = await seed_history(
        db_session, (await _learner(db_session)).id, replace(SMALL, sources=48, chunks_per_source=1)
    )
    with count_queries(db_session) as s:
        await sources_svc.list_sources(db_session, small.learner_id, limit=3)
    with count_queries(db_session) as big:
        await sources_svc.list_sources(db_session, large.learner_id, limit=3)
    assert len(big) <= len(s) + 2, big
    assert big.rows <= s.rows + 2, big


async def test_ties_at_a_page_boundary_are_neither_skipped_nor_repeated(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    stamp = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        db_session.add(
            Source(
                learner_id=learner.id,
                kind="file",
                origin=f"f{i}.txt",
                status="done",
                meta={},
                attempts=0,
                created_at=stamp,
            )
        )
    await db_session.flush()
    first, more = await sources_svc.list_sources(db_session, learner.id, limit=3)
    second, done = await sources_svc.list_sources(
        db_session, learner.id, limit=3, before=first[-1].id
    )
    assert more and not done
    assert len({s.id for s in first + second}) == 5


async def test_a_foreign_cursor_yields_the_first_page(db_session: AsyncSession) -> None:
    mine = await _learner(db_session)
    theirs = await _learner(db_session)
    await seed_history(db_session, mine.id, SMALL)
    foreign = await seed_history(db_session, theirs.id, SMALL)
    their_source = (await sources_svc.list_sources(db_session, foreign.learner_id, limit=1))[0][0]
    page, _ = await sources_svc.list_sources(db_session, mine.id, limit=2, before=their_source.id)
    first, _ = await sources_svc.list_sources(db_session, mine.id, limit=2)
    assert [s.id for s in page] == [s.id for s in first]


async def test_the_endpoint_returns_a_bounded_page(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    await seed_history(
        db_session, api_learner.id, replace(SMALL, sources=120, chunks_per_source=1)
    )  # 120 sources
    body = (await api_client.get(f"{API}/sources")).json()
    assert len(body["sources"]) == 100
    assert body["has_more"] is True
    rest = (
        await api_client.get(f"{API}/sources", params={"before": body["sources"][-1]["id"]})
    ).json()
    assert len(rest["sources"]) == 20
    assert rest["has_more"] is False
