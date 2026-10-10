"""No checkpoint outlives its conversation or its learner (S17, V12).

A paused practice or negotiation holds what the learner typed. Deleting the conversation or the
account used to leave it in the checkpoint tables for good; now the thread is erased with its
owner, and an erase the saver refuses is queued and retried like a refused blob.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqState

from app.agent import checkpointing
from app.core.config import get_settings
from app.models.chat import Conversation
from app.models.erasure import ErasureKind, PendingErasure
from app.models.learner import Learner
from app.services import onboarding_sessions, removal, retention
from app.storage.memory import InMemoryBlobStore
from app.workers import tasks
from tests.checkpoint_helpers import has_checkpoint, put_checkpoint


async def _learner_with_conversation(session: AsyncSession) -> tuple[Learner, Conversation]:
    learner = Learner(handle=f"ce-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.commit()
    return learner, conversation


async def _queued(session: AsyncSession, target: str) -> PendingErasure | None:
    return await session.scalar(
        select(PendingErasure)
        .where(PendingErasure.kind == ErasureKind.CHECKPOINT, PendingErasure.target == target)
        .execution_options(populate_existing=True)
    )


async def test_erase_threads_deletes_through_the_saver(durable_checkpointer: None) -> None:
    thread = f"t-{uuid.uuid4().hex}"
    await put_checkpoint(thread)
    assert await checkpointing.erase_threads([thread]) == []
    assert not await has_checkpoint(thread)


async def test_a_volatile_saver_refuses_every_erase() -> None:
    """A process running volatile cannot reach the durable rows, so it must not claim it did."""
    await checkpointing.stop()
    assert await checkpointing.erase_threads(["a", "b"]) == ["a", "b"]


async def test_deleting_a_conversation_erases_its_thread(
    db_session: AsyncSession, durable_checkpointer: None
) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    await put_checkpoint(str(conversation.id))

    await removal.delete_conversation(db_session, learner.id, conversation.id, forget=False)

    assert not await has_checkpoint(str(conversation.id))
    assert await _queued(db_session, str(conversation.id)) is None


async def test_a_volatile_delete_is_queued_and_retried(db_session: AsyncSession) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    await checkpointing.stop()  # volatile

    await removal.delete_conversation(db_session, learner.id, conversation.id, forget=False)
    row = await _queued(db_session, str(conversation.id))
    assert row is not None

    now = datetime.now(UTC) + timedelta(seconds=1)
    assert await retention.retry_erasures(db_session, InMemoryBlobStore(), None, now=now) == 0
    row = await _queued(db_session, str(conversation.id))
    assert row is not None and row.attempts == 1  # still volatile: refused, backed off

    await checkpointing.start(get_settings())
    try:
        await put_checkpoint(str(conversation.id))
        later = now + timedelta(days=1)
        assert await retention.retry_erasures(db_session, InMemoryBlobStore(), None, now=later) == 1
        assert not await has_checkpoint(str(conversation.id))
        assert await _queued(db_session, str(conversation.id)) is None
    finally:
        await checkpointing.stop()


async def test_erasing_an_account_erases_every_thread_it_had(
    db_session: AsyncSession, durable_checkpointer: None
) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    onboarding_thread = onboarding_sessions.thread_key(record.session_id, learner.id)
    await put_checkpoint(str(conversation.id))
    await put_checkpoint(onboarding_thread)

    await retention.erase_learner(db_session, InMemoryBlobStore(), None, learner.id)

    assert not await has_checkpoint(str(conversation.id))
    assert not await has_checkpoint(onboarding_thread)


async def test_an_account_erased_while_volatile_queues_its_threads(
    db_session: AsyncSession,
) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    await checkpointing.stop()

    report = await retention.erase_learner(db_session, InMemoryBlobStore(), None, learner.id)

    expected = {str(conversation.id), onboarding_sessions.thread_key(record.session_id, learner.id)}
    assert set(report.threads_failed) == expected
    for thread in expected:
        assert await _queued(db_session, thread) is not None


async def test_the_worker_opens_the_checkpointer_even_without_the_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erasure retries run in the worker; without a durable saver every one is refused."""
    no_purge = get_settings().model_copy(update={"checkpoint_purge_interval_seconds": 0})
    monkeypatch.setattr(tasks, "get_settings", lambda: no_purge)
    state = TaskiqState()
    await checkpointing.stop()
    try:
        await tasks._start_checkpoint_purge(state)
        assert checkpointing.is_durable() is True
    finally:
        await tasks._stop_checkpoint_purge(state)
