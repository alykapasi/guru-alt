"""Pausing, resuming, and skipping guided practice without inventing evidence (S52).

Guided practice used to be all-or-nothing: once a question was posed, the only way through it
was to answer it. A learner who wanted to ask "wait, what does that even mean?" had nowhere to
put the question — the workflow graph's ``await_response`` interrupt takes exactly one shape,
a reply to grade, and anything else sent while it waits would either be misread as an attempt
or simply dropped. A side discussion and a wrong answer must not be the same event.

So a paused workflow gets its own phase (``ConversationPhase.PRACTICE_PAUSED``) rather than
being inferred from "the learner said something that wasn't an answer". Whether a message sent
during the pause is a real side question or something that should resume the question is
:func:`classify_paused_message`'s job, through the same intent gate the plain-chat check
already uses (:mod:`app.learning.conversation_evidence`) — a gate whose default is deliberately
not neutral: an unreadable reply is a deferral, not an attempt.

**Why a skip is not a failure.** Declining a question and getting it wrong are opposite claims
about what the learner knows, and only one of them is evidence. Grading a skip as wrong would
tell the tracer the learner attempted the item and could not do it; the truth is they never
tried. So :func:`skip` writes nothing to ``learning_events`` and touches no FSRS schedule — the
question simply stops being asked, exactly as :func:`resume` finding the question no longer
current lets it lapse rather than forcing an answer to something the system has moved past.
:func:`pause` and :func:`resume` themselves make no grading decision at all: they only move the
conversation's recorded phase, the same durable bookkeeping ``Conversation.phase`` already does
for everything else the frontend needs to survive a reload.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.learning import conversation_evidence
from app.learning.conversation_evidence import TurnIntent
from app.llm.registry import LLMClient
from app.models.assessment import Item
from app.models.chat import Conversation, ConversationPhase
from app.services import assessment as assessment_svc
from app.services import workflow as workflow_svc
from app.services.llm_log import log_llm_call


class PracticeConflict(Exception):
    """The requested action does not fit the conversation's current state."""


@dataclass(frozen=True)
class PracticeState:
    """What a pause/resume/skip left the conversation waiting for."""

    phase: ConversationPhase
    item: Item | None
    prompt: str | None
    ended: bool


async def classify_paused_message(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    item_id: uuid.UUID,
    content: str,
) -> TurnIntent:
    """What a message sent while practice is paused does about the paused question.

    Reuses the plain-chat check's own gate rather than inventing a second one — a side question
    asked mid-practice and one asked mid-conversation are the same judgment call. A missing item
    (deleted since the pause began) resolves to :attr:`TurnIntent.DEFERRAL`, the gate's own
    default for everything it cannot decide.
    """
    item = await assessment_svc.get_item_for(
        session, item_id, learner_id=learner_id, subject_id=conversation.subject_id
    )
    if item is None:
        return TurnIntent.DEFERRAL

    intent, usage = await conversation_evidence.classify_intent(
        llm, question=item.stem, message=content
    )
    if usage.input_tokens or usage.output_tokens:
        await log_llm_call(
            learner_id=learner_id,
            conversation_id=conversation.id,
            role=conversation_evidence.CHECK_ROLE.value,
            spec=llm.spec(conversation_evidence.CHECK_ROLE),
            usage=usage,
        )
    return intent


async def pause(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, conversation: Conversation
) -> PracticeState:
    """Pause guided practice for a side discussion.

    Requires a live, still-current paused-or-awaiting question — pausing nothing is a conflict,
    not a no-op, because the caller believing it paused something it did not is exactly the
    state that would let a side reply slip through ungated.
    """
    item_id = await workflow_svc.paused_item_id(
        llm, session, conversation.id, learner_id=learner_id
    )
    if item_id is None or conversation.phase == ConversationPhase.PRACTICE_PAUSED:
        raise PracticeConflict("no live practice to pause")
    conversation.phase = ConversationPhase.PRACTICE_PAUSED
    conversation.active_item_id = item_id
    await session.commit()
    return PracticeState(
        phase=ConversationPhase.PRACTICE_PAUSED, item=None, prompt=None, ended=False
    )


async def resume(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, conversation: Conversation
) -> PracticeState:
    """Return to the paused question, or report that it no longer fits the plan.

    No LLM call and no message row either way — resuming is bookkeeping, not a turn. The
    question itself, if there still is one, is exactly what the learner left (the checkpoint's
    own ``last_message``), never a freshly regenerated one.
    """
    if conversation.phase != ConversationPhase.PRACTICE_PAUSED:
        raise PracticeConflict("practice is not paused")
    paused = await workflow_svc.paused_prompt(llm, session, conversation.id, learner_id=learner_id)
    if paused is None:
        # The plan moved on, or the item is gone, while this was paused — the same staleness
        # check a resume-without-a-pause already applies. Ending here, rather than forcing an
        # answer, is what makes that check meaningful for a paused question too.
        conversation.phase = ConversationPhase.CHATTING
        conversation.active_item_id = None
        conversation.practice_scaffolds = 0
        await session.commit()
        return PracticeState(phase=ConversationPhase.CHATTING, item=None, prompt=None, ended=True)
    prompt, item = paused
    conversation.phase = ConversationPhase.AWAITING_ANSWER
    conversation.active_item_id = item.id
    await session.commit()
    return PracticeState(
        phase=ConversationPhase.AWAITING_ANSWER, item=item, prompt=prompt, ended=False
    )


async def skip(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, conversation: Conversation
) -> PracticeState:
    """Leave the question behind without answering it — never a failed attempt.

    Valid from either an open question (``AWAITING_ANSWER``) or a paused one
    (``PRACTICE_PAUSED``): a learner can decide they would rather move on in either state, and
    the outcome is identical either way. Writes no ``LearningEvent`` and touches no FSRS
    schedule — see the module docstring on why a skip is not evidence.
    """
    if conversation.phase not in (
        ConversationPhase.AWAITING_ANSWER,
        ConversationPhase.PRACTICE_PAUSED,
    ):
        raise PracticeConflict("no practice in progress to skip")
    # Harmless when there is no thread (e.g. a paused-but-already-stale checkpoint that
    # `paused_item_id` already discarded) — `discard_thread` is best-effort by design.
    await checkpointing.discard_thread(str(conversation.id))
    conversation.phase = ConversationPhase.CHATTING
    conversation.active_item_id = None
    conversation.active_item_scaffolds = 0
    conversation.practice_scaffolds = 0
    await session.commit()
    # Not `ended`: that flag means a resume found the question stale. A skip is the learner's
    # own choice, and the client already knows it made one.
    return PracticeState(phase=ConversationPhase.CHATTING, item=None, prompt=None, ended=False)
