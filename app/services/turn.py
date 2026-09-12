"""The durable lifecycle of one conversation turn (S51).

A turn used to exist only as a request in flight. The learner's message committed before
generation began and the assistant's only after streaming finished; nothing recorded the gap.
A disconnect, a worker restart, or a provider failure in between therefore left a question
with no answer and no explanation — and the client, which treats an ended stream as a finished
one, showed it as a normal reply.

This module records the attempt itself: opened and committed *before* generation, closed on
every path out of it, and identified by the client's own key so a retry means *this* turn
again rather than a second turn saying the same thing.

**Liveness.** A ``PENDING`` row whose conversation holds no ``turn_lock`` claim is dead, not
slow: the claim is held for the whole stream, so its absence means the request that opened the
turn is gone. That inference is exactly as process-local as the claim it reads — the same
constraint ``app/services/turn_lock.py`` already documents, and for the same reason (the
graphs' checkpointers are in-memory, so a paused turn cannot outlive its process anyway).
When those become durable, this liveness signal moves with them.
"""

import uuid
from collections.abc import Sequence

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Message, Turn, TurnStatus
from app.services import turn_lock
from app.services.turn_common import add_message

log = structlog.get_logger(__name__)


class TurnAlreadyCompleted(Exception):
    """This client turn key already produced a reply; replaying it would duplicate the turn."""


def history_without(history: Sequence[Message], turn: Turn | None) -> Sequence[Message]:
    """``history`` with the learner message ``turn`` already wrote removed.

    A retry finds its own message in the transcript, but every flow appends the turn's content
    to the model context itself — so without this the model is shown the question twice, and
    the "is this conversation empty?" test that routes a first turn to the refinement gate
    would route the retry of that same first turn somewhere else.
    """
    if turn is None or turn.user_message_id is None:
        return history
    return [m for m in history if m.id != turn.user_message_id]


async def reap_stale(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    """Mark this conversation's abandoned ``PENDING`` turns ``CANCELLED``; return how many.

    Called on the read and send paths rather than by a sweeper: a stranded turn only matters
    when someone looks at the conversation, and the check is one indexed update.
    """
    if await turn_lock.is_active(session, conversation_id):
        return 0
    result = await session.execute(
        update(Turn)
        .where(Turn.conversation_id == conversation_id, Turn.status == TurnStatus.PENDING)
        .values(status=TurnStatus.CANCELLED, error="the turn ended without completing")
        .returning(Turn.id)
    )
    reaped = len(result.all())
    if reaped:
        await session.commit()
        log.info("turn.reaped_stale", conversation_id=str(conversation_id), count=reaped)
    return reaped


async def resolve_client_turn(
    session: AsyncSession, conversation_id: uuid.UUID, client_turn_id: uuid.UUID | None
) -> Turn | None:
    """The turn this client key already opened, if any — the retry's own row.

    Raises :class:`TurnAlreadyCompleted` when that turn already produced a reply: replaying it
    would answer the same message twice, which is the duplicate this key exists to prevent.
    """
    if client_turn_id is None:
        return None
    turn = await session.scalar(
        select(Turn).where(
            Turn.conversation_id == conversation_id, Turn.client_turn_id == client_turn_id
        )
    )
    if turn is not None and turn.status == TurnStatus.COMPLETED:
        raise TurnAlreadyCompleted(str(turn.id))
    return turn


async def open_turn(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    flow: str,
    content: str,
    client_turn_id: uuid.UUID | None = None,
    existing: Turn | None = None,
) -> Turn:
    """Record the attempt and the learner's message, committed before generation starts.

    ``existing`` (from :func:`resolve_client_turn`) makes a retry reuse the same row and the
    same learner message rather than appending a second copy of the question.
    """
    if existing is not None:
        existing.status = TurnStatus.PENDING
        existing.flow = flow
        existing.error = None
        existing.assistant_message_id = None
        if existing.user_message_id is None:
            message = await add_message(session, conversation_id, "user", existing.content)
            existing.user_message_id = message.id
        await session.commit()
        return existing

    turn = Turn(
        conversation_id=conversation_id,
        client_turn_id=client_turn_id,
        flow=flow,
        content=content,
        status=TurnStatus.PENDING,
    )
    session.add(turn)
    message = await add_message(session, conversation_id, "user", content)
    turn.user_message_id = message.id
    await session.commit()
    return turn


async def close_turn(
    session: AsyncSession,
    turn_id: uuid.UUID,
    status: TurnStatus,
    *,
    assistant_message_id: uuid.UUID | None = None,
    error: str | None = None,
) -> None:
    """Record how the turn ended. Best-effort, like ``chat.record_phase``.

    A turn that has already streamed its whole reply must not fail because its bookkeeping
    write did; a status that fails to land is reaped as ``CANCELLED`` on the next read, which
    understates a completed turn but never invents one.
    """
    try:
        await session.execute(
            update(Turn)
            .where(Turn.id == turn_id)
            .values(status=status, assistant_message_id=assistant_message_id, error=error)
        )
        await session.commit()
    except Exception:
        log.warning("turn.status_not_recorded", turn_id=str(turn_id), status=status.value)


async def recent_turns(
    session: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 5
) -> Sequence[Turn]:
    """The conversation's most recent turns, newest first, after reaping stale ones.

    This is what makes an interruption visible: the client reads a real status instead of
    inferring one from a transcript that simply stops.
    """
    await reap_stale(session, conversation_id)
    result = await session.scalars(
        select(Turn)
        .where(Turn.conversation_id == conversation_id)
        .order_by(Turn.created_at.desc(), Turn.id.desc())
        .limit(limit)
    )
    return result.all()
