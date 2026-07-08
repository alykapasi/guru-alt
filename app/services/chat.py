"""Conversation CRUD + helpers for the chat turn.

Mutators here ``flush`` (so ids are assigned) but do not ``commit`` — ``create_conversation``
and ``run_tutor_turn`` own their own commit boundaries around streaming.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tutor import TutorState, build_tutor_graph
from app.llm.pricing import cost_usd
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.models.chat import Conversation, LLMCall, Message

log = structlog.get_logger(__name__)

TUTOR_SYSTEM_PROMPT = (
    "You are Guru, a patient and encouraging tutor. Explain ideas clearly and "
    "concisely, check the learner's understanding with questions, and prefer worked "
    "examples over lecturing. Adapt to the learner's level."
)


async def create_conversation(
    session: AsyncSession, learner_id: uuid.UUID, title: str | None = None
) -> Conversation:
    conversation = Conversation(learner_id=learner_id, title=title)
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


async def add_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    model: str | None = None,
) -> Message:
    message = Message(conversation_id=conversation_id, role=role, content=content, model=model)
    session.add(message)
    await session.flush()
    return message


def to_chat_messages(history: Sequence[Message]) -> list[ChatMessage]:
    """Convert persisted turns into provider-agnostic chat messages."""
    return [
        ChatMessage(role=ChatRole(m.role), content=m.content)
        for m in history
        if m.role in (ChatRole.USER, ChatRole.ASSISTANT)
    ]


async def record_llm_call(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    role: str,
    provider: str,
    model: str,
    usage: Usage,
    cost_usd: float,
) -> LLMCall:
    call = LLMCall(
        learner_id=learner_id,
        conversation_id=conversation_id,
        role=role,
        provider=provider,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost_usd,
    )
    session.add(call)
    await session.flush()
    return call


@dataclass(frozen=True)
class TurnEvent:
    """A streamed step of a tutor turn. The router maps these to SSE frames."""

    type: Literal["token", "error", "done"]
    text: str = ""
    detail: str = ""
    message_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0


async def run_tutor_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    history: Sequence[Message],
    user_content: str,
    max_tokens: int,
) -> AsyncIterator[TurnEvent]:
    """Persist the user turn, stream the tutor's reply through the graph, then persist it."""
    messages = to_chat_messages(history)
    messages.append(ChatMessage(role=ChatRole.USER, content=user_content))
    await add_message(session, conversation_id, ChatRole.USER.value, user_content)
    await session.commit()

    spec = llm.spec(ModelRole.SMART)
    initial: TutorState = {
        "messages": messages,
        "system": TUTOR_SYSTEM_PROMPT,
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
    yield TurnEvent(type="done", message_id=str(assistant.id), usage=usage, cost_usd=cost)
