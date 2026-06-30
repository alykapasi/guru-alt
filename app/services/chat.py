"""Conversation CRUD + helpers for the chat turn.

Mutators here ``flush`` (so ids are assigned) but do not ``commit`` — the endpoint
controls commit boundaries around streaming. ``create_conversation`` is standalone and
commits itself.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.types import ChatMessage, ChatRole, Usage
from app.models.chat import Conversation, LLMCall, Message

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
