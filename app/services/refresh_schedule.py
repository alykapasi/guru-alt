"""What is due for memory write-back and profile refresh, and claiming it once (S43).

Both used to run only when something asked: write-back when a client called an endpoint no
screen calls, the profile when the learner pressed Refresh. This decides, from the data alone,
what has gone quiet with evidence nothing has read yet — so a restart or a learner back after
weeks is caught up by the next pass, and there is no separate catch-up job to forget.

"Quiet" is measured from the newest message of any kind, so a conversation mid-reply is never
picked up. Administrator messages never make anything due: they are not the learner's evidence.
A claim is a conditional update of an attempt stamp, so two workers never queue the same item;
the job clears the stamp when it succeeds, and a failure waits ``refresh_retry_minutes``.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.profile import LearnerProfile


def utcnow() -> datetime:
    """Now, in the naive UTC these tables store."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Claimed:
    conversations: list[uuid.UUID]
    learners: list[uuid.UUID]


def _learners_own_message():
    return (
        Message.role == "user",
        Message.admin_actor_id.is_(None),
        Message.admin_action_id.is_(None),
    )


_ACTIVE = (Learner.deletion_requested_at.is_(None), Learner.suspended_at.is_(None))


async def due_conversations(
    session: AsyncSession,
    *,
    quiet_before: datetime,
    retry_before: datetime | None,
    limit: int | None,
) -> list[uuid.UUID]:
    """Conversations with learner messages newer than their watermark, quiet since
    ``quiet_before``, oldest unprocessed first. ``retry_before=None`` ignores claims."""
    newest_own = (
        select(Message.conversation_id, func.max(Message.created_at).label("at"))
        .where(*_learners_own_message())
        .group_by(Message.conversation_id)
        .subquery()
    )
    newest_any = (
        select(Message.conversation_id, func.max(Message.created_at).label("at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    stmt = (
        select(Conversation.id)
        .join(Learner, Learner.id == Conversation.learner_id)
        .join(newest_own, newest_own.c.conversation_id == Conversation.id)
        .join(newest_any, newest_any.c.conversation_id == Conversation.id)
        .where(
            or_(
                Conversation.memory_watermark.is_(None),
                newest_own.c.at > Conversation.memory_watermark,
            ),
            newest_any.c.at <= quiet_before,
            Conversation.archived_at.is_(None),
            Learner.remember_conversations.is_(True),
            *_ACTIVE,
        )
        .order_by(newest_own.c.at, Conversation.id)
    )
    if retry_before is not None:
        stmt = stmt.where(
            or_(
                Conversation.memory_attempted_at.is_(None),
                Conversation.memory_attempted_at < retry_before,
            )
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.scalars(stmt)).all())


async def due_learners(
    session: AsyncSession,
    *,
    quiet_before: datetime,
    retry_before: datetime | None,
    limit: int | None,
) -> list[uuid.UUID]:
    """Learners whose newest evidence — a graded answer or a message they wrote — is newer
    than their profile's watermark and older than ``quiet_before``, oldest first."""
    evidence = union_all(
        select(
            LearningEvent.learner_id.label("learner_id"), LearningEvent.created_at.label("at")
        ).where(LearningEvent.event_type == "observation"),
        select(Conversation.learner_id.label("learner_id"), Message.created_at.label("at"))
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*_learners_own_message()),
    ).subquery()
    newest = (
        select(evidence.c.learner_id, func.max(evidence.c.at).label("at"))
        .group_by(evidence.c.learner_id)
        .subquery()
    )
    stmt = (
        select(Learner.id)
        .join(newest, newest.c.learner_id == Learner.id)
        .outerjoin(LearnerProfile, LearnerProfile.learner_id == Learner.id)
        .where(
            newest.c.at <= quiet_before,
            or_(
                LearnerProfile.evidence_watermark.is_(None),
                newest.c.at > LearnerProfile.evidence_watermark,
            ),
            *_ACTIVE,
        )
        .order_by(newest.c.at, Learner.id)
    )
    if retry_before is not None:
        stmt = stmt.where(
            or_(
                LearnerProfile.refresh_attempted_at.is_(None),
                LearnerProfile.refresh_attempted_at < retry_before,
            )
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.scalars(stmt)).all())


async def _claim_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, *, now: datetime, retry_before: datetime
) -> bool:
    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            or_(
                Conversation.memory_attempted_at.is_(None),
                Conversation.memory_attempted_at < retry_before,
            ),
        )
        .values(memory_attempted_at=now)
        .execution_options(synchronize_session=False)
    )
    return cast("CursorResult[Any]", result).rowcount == 1


async def _claim_learner(
    session: AsyncSession, learner_id: uuid.UUID, *, now: datetime, retry_before: datetime
) -> bool:
    # A learner with evidence and no profile yet is due; the claim needs a row to stamp.
    await session.execute(
        insert(LearnerProfile)
        .values(id=uuid.uuid4(), learner_id=learner_id)
        .on_conflict_do_nothing(index_elements=["learner_id"])
    )
    result = await session.execute(
        update(LearnerProfile)
        .where(
            LearnerProfile.learner_id == learner_id,
            or_(
                LearnerProfile.refresh_attempted_at.is_(None),
                LearnerProfile.refresh_attempted_at < retry_before,
            ),
        )
        .values(refresh_attempted_at=now)
        .execution_options(synchronize_session=False)
    )
    return cast("CursorResult[Any]", result).rowcount == 1


async def claim_due(session: AsyncSession, *, now: datetime, settings: Settings) -> Claimed:
    """Claim up to ``refresh_batch_size`` conversations and learners that are due, and commit."""
    quiet_before = now - timedelta(minutes=settings.memory_quiet_minutes)
    retry_before = now - timedelta(minutes=settings.refresh_retry_minutes)
    conversations = [
        conversation_id
        for conversation_id in await due_conversations(
            session,
            quiet_before=quiet_before,
            retry_before=retry_before,
            limit=settings.refresh_batch_size,
        )
        if await _claim_conversation(session, conversation_id, now=now, retry_before=retry_before)
    ]
    learners = [
        learner_id
        for learner_id in await due_learners(
            session,
            quiet_before=quiet_before,
            retry_before=retry_before,
            limit=settings.refresh_batch_size,
        )
        if await _claim_learner(session, learner_id, now=now, retry_before=retry_before)
    ]
    await session.commit()
    return Claimed(conversations=conversations, learners=learners)


async def stuck(session: AsyncSession, *, now: datetime, settings: Settings) -> int:
    """How many conversations and learners have been due for over ``refresh_stuck_hours``."""
    before = now - timedelta(hours=settings.refresh_stuck_hours)
    conversations = await due_conversations(
        session, quiet_before=before, retry_before=None, limit=None
    )
    learners = await due_learners(session, quiet_before=before, retry_before=None, limit=None)
    return len(conversations) + len(learners)
