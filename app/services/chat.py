"""Conversation CRUD + the plain tutor turn.

Mutators here ``flush`` (so ids are assigned) but do not ``commit`` — ``create_conversation``
and ``run_tutor_turn`` own their own commit boundaries around streaming.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tutor import TutorState, build_tutor_graph
from app.core.config import get_settings
from app.llm.pricing import cost_usd
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.memory import retrieval as memory_retrieval
from app.memory.retrieval import MemoryHit
from app.models.chat import Conversation, Message
from app.services import session_runner as session_runner_svc
from app.services.assessment import item_to_read
from app.services.lesson_plan import PlanGroundingContext, get_active_step_context
from app.services.turn_common import TurnEvent, add_message, record_llm_call, to_chat_messages

log = structlog.get_logger(__name__)

TUTOR_SYSTEM_PROMPT = (
    "You are Guru, a patient and encouraging tutor. Explain ideas clearly and "
    "concisely, check the learner's understanding with questions, and prefer worked "
    "examples over lecturing. Adapt to the learner's level."
)


def _plan_grounding_note(context: PlanGroundingContext) -> str:
    parts = [
        f"The learner's current lesson-plan focus in {context.subject_name}: {context.kc_name}."
    ]
    if context.target_difficulty is not None:
        parts.append(f"Target difficulty: {context.target_difficulty:.2f}.")
    if context.hint_density is not None:
        parts.append(f"Hint density: {context.hint_density}.")
    if context.preferred_item_type is not None:
        parts.append(f"Preferred item type: {context.preferred_item_type}.")
    return " ".join(parts)


def _memory_note(hits: Sequence[MemoryHit]) -> str:
    facts = "; ".join(f"[{h.kind}] {h.content}" for h in hits)
    return f"What you remember about this learner from past conversations: {facts}."


async def create_conversation(
    session: AsyncSession,
    learner_id: uuid.UUID,
    title: str | None = None,
    subject_id: uuid.UUID | None = None,
) -> Conversation:
    conversation = Conversation(learner_id=learner_id, title=title, subject_id=subject_id)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


async def list_conversations(
    session: AsyncSession, learner_id: uuid.UUID
) -> Sequence[Conversation]:
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.learner_id == learner_id)
        .order_by(Conversation.created_at.desc())
    )
    return result.all()


async def list_messages(session: AsyncSession, conversation_id: uuid.UUID) -> Sequence[Message]:
    result = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return result.all()


async def run_tutor_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    history: Sequence[Message],
    user_content: str,
    max_tokens: int,
    goal: str | None = None,
    subject_id: uuid.UUID | None = None,
) -> AsyncIterator[TurnEvent]:
    """Persist the user turn, stream the tutor's reply through the graph, then persist it.

    ``goal`` is the conversation's committed goal from the refinement gate (if any) —
    folded into the system prompt so generation stays grounded in it. The learner's active
    lesson-plan step (if any) is folded in the same way, so the plan actually drives the
    conversation rather than sitting beside it — see ``lesson_plan.get_active_step_context``.

    ``subject_id`` (the conversation's, if scoped to one) makes that lookup exact instead of
    the cross-subject heuristic, and additionally resolves a practice item for the active step
    (see ``session_runner.next_item``) attached to the "done" event — the session runner
    following the plan, not just talking about it.

    Learner-global memory (facts/preferences/summaries from past conversations — see
    ``app.memory.retrieval``) is folded in on every turn, not gated behind ``subject_id``; this
    is what "wires memory into sessions" — write-back is a separate, on-demand step (see
    ``app.services.memory.write_back``). System-prompt order is pinned: base prompt -> goal ->
    plan-grounding -> memory-note.
    """
    messages = to_chat_messages(history)
    messages.append(ChatMessage(role=ChatRole.USER, content=user_content))
    await add_message(session, conversation_id, ChatRole.USER.value, user_content)
    await session.commit()

    system = TUTOR_SYSTEM_PROMPT
    if goal:
        system = f"{TUTOR_SYSTEM_PROMPT}\n\nThe learner's stated goal for this conversation: {goal}"
    plan_context = await get_active_step_context(session, learner_id, subject_id=subject_id)
    if plan_context is not None:
        system = f"{system}\n\n{_plan_grounding_note(plan_context)}"
    memory_hits = await memory_retrieval.retrieve(
        session,
        llm,
        user_content,
        learner_id=learner_id,
        limit=get_settings().memory_retrieval_limit,
    )
    if memory_hits:
        system = f"{system}\n\n{_memory_note(memory_hits)}"

    practice_item = None
    if subject_id is not None:
        practice_item = await session_runner_svc.next_item(
            session, llm, learner_id=learner_id, subject_id=subject_id
        )

    spec = llm.spec(ModelRole.SMART)
    initial: TutorState = {
        "messages": messages,
        "system": system,
        "max_tokens": max_tokens,
        "reply": "",
        "usage": Usage(),
    }

    reply = ""
    usage = Usage()
    try:
        async for mode, payload in build_tutor_graph(llm).astream(
            initial, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield TurnEvent(type="token", text=payload["token"])  # ty: ignore[invalid-argument-type]
            elif mode == "values":
                reply = payload["reply"]  # ty: ignore[invalid-argument-type]
                usage = payload["usage"]  # ty: ignore[invalid-argument-type]
    except Exception as exc:
        log.error("tutor.stream_failed", error=str(exc), model=spec.model)
        yield TurnEvent(type="error", detail="generation failed")
        return

    assistant = await add_message(
        session, conversation_id, ChatRole.ASSISTANT.value, reply, model=spec.model
    )
    cost = cost_usd(spec.model, usage)
    await record_llm_call(
        session,
        learner_id=learner_id,
        conversation_id=conversation_id,
        role=ModelRole.SMART.value,
        provider=spec.provider,
        model=spec.model,
        usage=usage,
        cost_usd=cost,
    )
    await session.commit()
    log.info(
        "llm.call",
        role=ModelRole.SMART.value,
        provider=spec.provider,
        model=spec.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )
    item_read = item_to_read(practice_item) if practice_item is not None else None
    yield TurnEvent(
        type="done", message_id=str(assistant.id), usage=usage, cost_usd=cost, item=item_read
    )
