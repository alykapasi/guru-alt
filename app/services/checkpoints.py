"""Ending the durable state S17 started keeping.

S17 made a paused conversation survive a restart, which was the right fix and only half a
lifecycle: nothing ever *ended* one. The checkpoint table only grew, so a practice abandoned in
March is still resumable state in September, and there is no moment at which the system decides
a question is no longer worth asking.

Two different jobs, and they are not the same job:

*Pruning* is about rows nobody will come back for. A conversation with no activity for weeks is
not paused, it is abandoned, and its checkpoint is storage with no reader.

*Revalidation* is about rows somebody **does** come back for, which is the more interesting
case and the one durability created. A volatile checkpoint could not outlive much, so the
question it held was never very stale. A durable one outlives the plan revision that changed
what the learner should be doing, the detour that sent them somewhere else, and the mastery
they picked up on another path — and resuming it puts a question in front of them that the
system itself no longer thinks they should be answering.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.types import StateSnapshot
from psycopg import OperationalError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.core.db import engine_of
from app.models.chat import Conversation, Message, OnboardingSession
from app.services import onboarding_sessions, turn_lock
from app.services.assessment import get_item_for
from app.services.lesson_plan import mastered_kc_ids

log = structlog.get_logger(__name__)


async def stale_conversation_ids(
    session: AsyncSession, *, older_than: timedelta
) -> list[uuid.UUID]:
    """Conversations whose most recent message predates the cutoff.

    Last *message*, not the conversation's own ``updated_at``: a row can be touched by a phase
    write or a title change without anybody having said anything, and the question here is
    whether a person is still in this conversation.

    A conversation with no messages at all counts by its creation time, which is what catches
    one opened and abandoned before a word was said.
    """
    # Naive UTC, because ``created_at`` is ``TIMESTAMP WITHOUT TIME ZONE`` — the same
    # conversion every other window query in this codebase makes (see ``services.spend``).
    cutoff = (datetime.now(UTC) - older_than).replace(tzinfo=None)
    last_message = (
        select(func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = await session.scalars(
        select(Conversation.id).where(func.coalesce(last_message, Conversation.created_at) < cutoff)
    )
    return list(rows)


async def _discard_unless_busy(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Discard a conversation's thread unless a turn is running in it right now.

    Taken through the same claim a turn takes, so a learner answering a weeks-old question at
    the moment the sweep runs keeps their question.
    """
    claim = await turn_lock.claim(engine_of(session), conversation_id)
    if claim is None:
        return False
    try:
        return await checkpointing.discard_thread(str(conversation_id))
    finally:
        await claim.release()


async def prune(session: AsyncSession, *, older_than: timedelta) -> int:
    """Discard paused state nobody has touched since the cutoff; returns threads discarded.

    Conversations: both graphs key their thread on the conversation id, so one discard covers
    whichever left state behind. Onboarding sessions: the row goes with its negotiation.
    """
    ids = await stale_conversation_ids(session, older_than=older_than)
    discarded = 0
    for conversation_id in ids:
        if await _discard_unless_busy(session, conversation_id):
            discarded += 1
    cutoff = (datetime.now(UTC) - older_than).replace(tzinfo=None)
    idle = (
        await session.execute(
            select(OnboardingSession.session_id, OnboardingSession.learner_id).where(
                OnboardingSession.updated_at < cutoff
            )
        )
    ).all()
    for session_id, learner_id in idle:
        await checkpointing.discard_thread(onboarding_sessions.thread_key(session_id, learner_id))
        await onboarding_sessions.forget(session, session_id)
        discarded += 1
    if discarded:
        log.info("checkpoints.pruned", threads=discarded, candidates=len(ids) + len(idle))
    return discarded


async def prune_orphans(session: AsyncSession) -> int:
    """Delete every thread whose conversation or onboarding session no longer exists.

    The backstop for erasures that never ran — threads from before S17's erasure, or a crash
    between a delete's commit and its erase. Reads thread ids with plain SQL; deletes through
    the saver, so the library still owns every write to its tables. An owner row is always
    committed before its thread is first written, so a thread created during the sweep is
    never mistaken for an orphan.
    """
    if not checkpointing.is_durable():
        return 0
    threads = list(await session.scalars(text("SELECT DISTINCT thread_id FROM checkpoints")))
    conversations: dict[uuid.UUID, str] = {}
    negotiations: dict[tuple[uuid.UUID, str], str] = {}
    orphans: list[str] = []
    for thread in threads:
        learner_part, colon, session_part = thread.partition(":")
        try:
            if colon:
                negotiations[(uuid.UUID(learner_part), session_part)] = thread
            else:
                conversations[uuid.UUID(thread)] = thread
        except ValueError:
            orphans.append(thread)
    live_conversations = set(
        await session.scalars(select(Conversation.id).where(Conversation.id.in_(conversations)))
    )
    live_negotiations = {
        (row.learner_id, row.session_id)
        for row in await session.execute(
            select(OnboardingSession.learner_id, OnboardingSession.session_id).where(
                OnboardingSession.session_id.in_([sid for _lid, sid in negotiations])
            )
        )
    }
    orphans += [t for cid, t in conversations.items() if cid not in live_conversations]
    orphans += [t for key, t in negotiations.items() if key not in live_negotiations]
    failed = await checkpointing.erase_threads(orphans)
    swept = len(orphans) - len(failed)
    if swept:
        log.info("checkpoints.orphans_swept", threads=swept)
    return swept


def compatible(snapshot: StateSnapshot, version: int) -> bool:
    """Whether a checkpoint was written by the graph shape this code resumes."""
    return (snapshot.metadata or {}).get("graph_version") == version


async def paused_state(
    graph: Any, config: RunnableConfig, *, graph_name: str, version: int
) -> StateSnapshot | None:
    """The paused snapshot this code can resume, or ``None``.

    ``None`` when nothing is paused, and when something is but this code cannot resume it — a
    checkpoint from another graph version, an unstamped one from before versions existed, or
    one that cannot be read at all. Those are discarded here, once, so the caller's fallback
    (ordinary chat, no negotiation, an expired session) is what the learner gets.
    """
    thread_id = config["configurable"]["thread_id"]
    found: object
    try:
        snapshot = await graph.aget_state(config)
    except OperationalError:
        # The database, not the checkpoint (a saturated pool, a failover): the question is
        # still there, so the request fails the way any other would and nothing is dropped.
        raise
    except Exception:
        found = "unreadable"
    else:
        if not snapshot.next:
            return None
        if compatible(snapshot, version):
            return snapshot
        found = (snapshot.metadata or {}).get("graph_version")
    log.warning(
        "checkpointer.incompatible_dropped",
        graph=graph_name,
        expected=version,
        found=found,
        thread_id=thread_id,
    )
    await checkpointing.discard_thread(thread_id)
    return None


async def paused_practice_is_current(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    item_id: str | None,
    subject_id: uuid.UUID | None = None,
) -> bool:
    """Whether a paused practice question is still one worth putting back in front of somebody.

    Two ways it stops being current, and both became reachable the day the checkpoint outlived
    the process:

    *The item is gone.* Deleted, or never resolvable — there is nothing to grade an answer
    against, so resuming would take an answer and lose it.

    *They have since mastered the component.* Through a detour, another session, or the same
    component in a different conversation. Asking somebody to demonstrate what the tracer
    already records as established is not a test, and a wrong answer to it would move a
    settled estimate on evidence the system did not think it needed.

    Deliberately **not** checked: whether the item is still the plan's active step. Plans are
    revised after every graded answer, and a question can be worth finishing even after the
    plan has moved past it — the learner is mid-thought. Mastery is the line, because that is
    the point at which asking has stopped being informative rather than merely untidy.
    """
    if not item_id:
        return False
    try:
        parsed = uuid.UUID(item_id)
    except ValueError:
        return False
    # Scoped to the learner, and eager-loading the components: ``get_item_for`` answers "may
    # this learner still be assessed with this" (S33), which is the right question for a
    # question that has been sitting around — an item can change hands while it waits.
    item = await get_item_for(session, parsed, learner_id=learner_id, subject_id=subject_id)
    if item is None:
        return False
    kc_ids = [link.kc_id for link in item.kc_links]
    if not kc_ids:
        return True  # nothing to have mastered; the question stands
    # The planner's own definition of mastered, not a second one: the two deciding differently
    # would mean a plan that has moved on and a resume that has not.
    mastered = await mastered_kc_ids(session, learner_id, kc_ids)
    return not all(kc_id in mastered for kc_id in kc_ids)
