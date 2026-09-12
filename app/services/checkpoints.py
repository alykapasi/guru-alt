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

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.models.chat import Conversation, Message
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


async def prune(session: AsyncSession, *, older_than: timedelta) -> int:
    """Discard checkpoints for conversations nobody has touched since the cutoff.

    Returns how many threads the saver accepted. Both graphs — the refinement gate and the
    practice loop — key their thread on the conversation id, so one discard per conversation
    covers whichever of them left state behind.
    """
    ids = await stale_conversation_ids(session, older_than=older_than)
    discarded = 0
    for conversation_id in ids:
        if await checkpointing.discard_thread(str(conversation_id)):
            discarded += 1
    if discarded:
        log.info("checkpoints.pruned", threads=discarded, candidates=len(ids))
    return discarded


async def paused_practice_is_current(
    session: AsyncSession, *, learner_id: uuid.UUID, item_id: str | None
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
    item = await get_item_for(session, parsed, learner_id=learner_id)
    if item is None:
        return False
    kc_ids = [link.kc_id for link in item.kc_links]
    if not kc_ids:
        return True  # nothing to have mastered; the question stands
    # The planner's own definition of mastered, not a second one: the two deciding differently
    # would mean a plan that has moved on and a resume that has not.
    mastered = await mastered_kc_ids(session, learner_id, kc_ids)
    return not all(kc_id in mastered for kc_id in kc_ids)
