"""The review queue's cost does not grow with how much is due (S62 part B)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.services import session_runner
from tests.history import SMALL, seed_history
from tests.querycount import count_queries


async def _learner_with_due(session: AsyncSession, topics: int) -> uuid.UUID:
    learner = Learner(handle=f"rq-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    shape = replace(SMALL, topics_per_subject=topics, events=0, vector_pool=8)
    await seed_history(session, learner.id, shape)
    # Everything due yesterday, so the queue is as long as the graph.
    await session.execute(
        update(LearnerKCState)
        .where(LearnerKCState.learner_id == learner.id)
        .values(due_at=datetime.now(UTC) - timedelta(days=1))
    )
    await session.flush()
    return learner.id


async def test_the_queue_costs_the_same_however_much_is_due(db_session: AsyncSession) -> None:
    few = await _learner_with_due(db_session, topics=1)
    many = await _learner_with_due(db_session, topics=4)

    with count_queries(db_session) as small:
        a = await session_runner.due_review_items(
            db_session, fake_llm_client(), learner_id=few, item_limit=0
        )
    with count_queries(db_session) as large:
        b = await session_runner.due_review_items(
            db_session, fake_llm_client(), learner_id=many, item_limit=0
        )

    assert len(b) > len(a) > 0
    assert len(large) <= len(small), f"small {small!r}\nlarge {large!r}"


async def test_a_foreign_component_is_skipped_in_one_query(db_session: AsyncSession) -> None:
    owner = await _learner_with_due(db_session, topics=1)
    other = await _learner_with_due(db_session, topics=1)
    # Make one of `other`'s subjects private to `owner`: its components leave other's queue.
    foreign = await db_session.scalar(select(Subject.id).where(Subject.owner_learner_id == other))
    await db_session.execute(
        update(Subject).where(Subject.id == foreign).values(owner_learner_id=owner)
    )
    await db_session.flush()

    queue = await session_runner.due_review_items(
        db_session, fake_llm_client(), learner_id=other, item_limit=0
    )

    assert queue == []
