"""Shared helpers for streamed conversation turns (plain tutoring + the refinement gate).

Split out from ``chat.py`` so ``refinement.py`` can reuse them without the two service
modules importing each other.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.types import ChatMessage, ChatRole, Usage
from app.models.chat import LLMCall, Message


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
    """A streamed step of a conversation turn. The router maps these to SSE frames."""

    type: Literal["token", "error", "done", "awaiting_reply", "committed"]
    text: str = ""
    detail: str = ""
    message_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
