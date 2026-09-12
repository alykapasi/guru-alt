"""Ending the durable state S17 started keeping.

S17 made a paused conversation survive a restart and gave nothing the job of ending one, so
the checkpoint table only grew. Two separate failures follow from that and they are not the
same failure: rows nobody will come back for, and rows somebody *does* come back for that no
longer describe anything the system wants to ask.
"""

import uuid
from datetime import UTC, datetime, timedelta

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.models.assessment import ItemType
from app.models.chat import Conversation, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.schemas.assessment import ItemCreate, ItemKCRef
from app.services import assessment as assessment_svc
from app.services import checkpoints as svc
from app.services.lesson_plan import MASTERY_ABILITY_THRESHOLD, MASTERY_UNCERTAINTY_THRESHOLD


def _naive(age: timedelta) -> datetime:
    """``created_at`` is ``TIMESTAMP WITHOUT TIME ZONE``, so a tz-aware value is rejected."""
    return (datetime.now(UTC) - age).replace(tzinfo=None)


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _kc(session: AsyncSession) -> KC:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="k", name="Velocity")
    session.add(kc)
    await session.flush()
    return kc


async def _conversation(session: AsyncSession, learner: Learner, *, age: timedelta) -> Conversation:
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            content="hello",
            created_at=_naive(age),
        )
    )
    await session.flush()
    return conversation


# --- pruning: rows nobody will come back for -------------------------------------------------


async def test_a_conversation_nobody_has_touched_for_weeks_is_a_candidate(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    stale = await _conversation(db_session, learner, age=timedelta(days=40))

    ids = await svc.stale_conversation_ids(db_session, older_than=timedelta(days=30))
    assert stale.id in ids


async def test_a_conversation_someone_is_still_in_is_left_alone(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    live = await _conversation(db_session, learner, age=timedelta(hours=2))

    ids = await svc.stale_conversation_ids(db_session, older_than=timedelta(days=30))
    assert live.id not in ids


async def test_recent_activity_rescues_an_old_conversation(db_session: AsyncSession) -> None:
    """The cutoff is the *last message*, not the conversation's age: a long-running thread
    somebody spoke in this morning is not abandoned."""
    learner = await _learner(db_session)
    old = await _conversation(db_session, learner, age=timedelta(days=60))
    db_session.add(Message(conversation_id=old.id, role="user", content="still here"))
    await db_session.flush()

    ids = await svc.stale_conversation_ids(db_session, older_than=timedelta(days=30))
    assert old.id not in ids


async def test_a_conversation_abandoned_before_a_word_was_said_still_counts(
    db_session: AsyncSession,
) -> None:
    """No messages at all, so there is no last message to compare — it falls back to when the
    conversation was opened rather than being skipped forever."""
    learner = await _learner(db_session)
    empty = Conversation(learner_id=learner.id, created_at=_naive(timedelta(days=90)))
    db_session.add(empty)
    await db_session.flush()

    ids = await svc.stale_conversation_ids(db_session, older_than=timedelta(days=30))
    assert empty.id in ids


async def test_pruning_discards_one_thread_per_stale_conversation(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    stale = await _conversation(db_session, learner, age=timedelta(days=40))
    await _conversation(db_session, learner, age=timedelta(hours=1))

    saver = checkpointing.checkpointer()
    config: RunnableConfig = {"configurable": {"thread_id": str(stale.id), "checkpoint_ns": ""}}
    checkpoint: Checkpoint = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": "",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    metadata: CheckpointMetadata = {"source": "update", "step": 1, "parents": {}}
    await saver.aput(config, checkpoint, metadata, {})
    assert [c async for c in saver.alist(config)], "the checkpoint should exist before pruning"

    discarded = await svc.prune(db_session, older_than=timedelta(days=30))
    assert discarded == 1
    assert [c async for c in saver.alist(config)] == []


# --- revalidation: rows somebody does come back for ------------------------------------------


async def _item_for(session: AsyncSession, kc: KC):
    return await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.SHORT, stem="Explain velocity.", kcs=[ItemKCRef(kc_id=kc.id)]
        ),
    )


async def test_an_ordinary_paused_question_is_still_worth_asking(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    item = await _item_for(db_session, kc)

    assert await svc.paused_practice_is_current(
        db_session, learner_id=learner.id, item_id=str(item.id)
    )


async def test_a_question_whose_item_is_gone_is_not_resumed(db_session: AsyncSession) -> None:
    """Nothing to grade an answer against, so resuming would take an answer and lose it."""
    learner = await _learner(db_session)

    assert not await svc.paused_practice_is_current(
        db_session, learner_id=learner.id, item_id=str(uuid.uuid4())
    )


async def test_a_component_mastered_in_the_meantime_closes_the_question(
    db_session: AsyncSession,
) -> None:
    """The case durability created. Asking somebody to demonstrate what the tracer already
    records as established is not a test, and a wrong answer would move a settled estimate."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    item = await _item_for(db_session, kc)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=kc.id,
            ability=MASTERY_ABILITY_THRESHOLD + 0.5,
            uncertainty=MASTERY_UNCERTAINTY_THRESHOLD - 0.1,
        )
    )
    await db_session.flush()

    assert not await svc.paused_practice_is_current(
        db_session, learner_id=learner.id, item_id=str(item.id)
    )


async def test_a_component_merely_going_well_does_not_close_it(db_session: AsyncSession) -> None:
    """The bar is the planner's own definition of mastered, not "doing all right" — otherwise
    a learner mid-exercise would have it taken away for answering the first part correctly."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    item = await _item_for(db_session, kc)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=kc.id,
            ability=MASTERY_ABILITY_THRESHOLD + 0.5,
            uncertainty=MASTERY_UNCERTAINTY_THRESHOLD + 0.3,
        )
    )
    await db_session.flush()

    assert await svc.paused_practice_is_current(
        db_session, learner_id=learner.id, item_id=str(item.id)
    )


async def test_a_checkpoint_holding_no_item_is_not_resumable(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    assert not await svc.paused_practice_is_current(db_session, learner_id=learner.id, item_id=None)
    assert not await svc.paused_practice_is_current(
        db_session, learner_id=learner.id, item_id="not-a-uuid"
    )


async def test_another_learners_item_does_not_resume(db_session: AsyncSession) -> None:
    """An item can change hands while a question sits paused (S33); "may this learner still be
    assessed with it" is the right question to ask of one that has been waiting."""
    learner = await _learner(db_session)
    other = await _learner(db_session)
    kc = await _kc(db_session)
    item = await assessment_svc.create_item(
        db_session,
        ItemCreate(
            item_type=ItemType.SHORT, stem="Explain velocity.", kcs=[ItemKCRef(kc_id=kc.id)]
        ),
        # Authored by the other learner, so it is theirs alone (S33) rather than shared bank.
        author_learner_id=other.id,
    )

    assert not await svc.paused_practice_is_current(
        db_session, learner_id=learner.id, item_id=str(item.id)
    )
