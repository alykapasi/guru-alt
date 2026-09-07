"""Per-learner memory: write-back (extract + dedup + persist) and view/erase.

``write_back`` is triggered on-demand (``POST /conversations/{id}/memory/write-back``, queued —
see ``app.workers.tasks.memory_write_back_task``), not automatically on every turn: extraction
costs one FAST call, and firing it per-message would pay that repeatedly for no accumulated
benefit (same reasoning already applied to ``POST /profile/refresh``). Retrieval
(``app.memory.retrieval``) is what actually "wires memory into sessions" — it runs on every
tutor turn instead, since it's cheap (one EMBED call + one DB query, same tier RAG retrieval
already pays).
"""

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ModelRole
from app.memory.extraction import EXTRACTION_ROLE, extract_memories
from app.models.chat import Conversation, Message
from app.models.memory import Memory, MemoryKind
from app.services.llm_log import log_llm_call
from app.services.turn_common import to_chat_messages


async def write_back(
    session: AsyncSession, llm: LLMClient, *, conversation_id: uuid.UUID
) -> list[Memory]:
    """Extract durable memories from ``conversation_id``'s recent history and persist them.

    Unknown/foreign ``conversation_id`` -> ``[]``, no crash (the queued task may run after the
    conversation's owner state has changed). Near-duplicate candidates (cosine distance to an
    existing same-``(learner_id, kind)`` memory at or below ``memory_dedup_max_distance``) are
    skipped rather than persisted.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return []

    settings = get_settings()
    window = await _recent_messages(
        session, conversation_id, window=settings.memory_extraction_window
    )
    extracted, usage = await extract_memories(llm, to_chat_messages(window))
    if usage.total_tokens:
        await log_llm_call(
            learner_id=conversation.learner_id,
            role=EXTRACTION_ROLE.value,
            spec=llm.spec(EXTRACTION_ROLE),
            usage=usage,
            conversation_id=conversation_id,
        )
    if not extracted:
        await session.commit()
        return []

    embedded = await llm.embed(ModelRole.EMBED, [item.content for item in extracted])
    if embedded.usage.total_tokens:
        await log_llm_call(
            learner_id=conversation.learner_id,
            role=str(ModelRole.EMBED),
            spec=llm.spec(ModelRole.EMBED),
            usage=embedded.usage,
            conversation_id=conversation_id,
        )
    created: list[Memory] = []
    for item, embedding in zip(extracted, embedded.vectors, strict=True):
        if await _is_near_duplicate(
            session,
            conversation.learner_id,
            item.kind,
            embedding,
            max_distance=settings.memory_dedup_max_distance,
        ):
            continue
        memory = Memory(
            learner_id=conversation.learner_id,
            conversation_id=conversation_id,
            kind=item.kind,
            content=item.content,
            embedding=embedding,
        )
        # Autoflush (the default) makes this visible to the *next* item's dedup query below —
        # intentional intra-batch dedup, not an accident. Don't collapse this into one batched
        # check; that would silently disable it.
        session.add(memory)
        created.append(memory)
    await session.commit()
    return created


async def _recent_messages(
    session: AsyncSession, conversation_id: uuid.UUID, *, window: int
) -> list[Message]:
    rows = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(window)
        )
    ).all()
    return list(reversed(rows))  # oldest-first, matching conversation reading order


async def _is_near_duplicate(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kind: MemoryKind,
    embedding: list[float],
    *,
    max_distance: float,
) -> bool:
    distance = Memory.embedding.cosine_distance(embedding)
    nearest = await session.scalar(
        select(distance)
        .where(Memory.learner_id == learner_id, Memory.kind == kind)
        .order_by(distance)
        .limit(1)
    )
    return nearest is not None and nearest <= max_distance


async def list_memories(
    session: AsyncSession, learner_id: uuid.UUID, *, limit: int = 50
) -> Sequence[Memory]:
    result = await session.scalars(
        select(Memory)
        .where(Memory.learner_id == learner_id)
        .order_by(Memory.created_at.desc())
        .limit(limit)
    )
    return result.all()


async def delete_memory(session: AsyncSession, learner_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
    """Delete one memory the learner owns. ``False`` (no-op) if missing or not theirs."""
    memory = await session.get(Memory, memory_id)
    if memory is None or memory.learner_id != learner_id:
        return False
    await session.delete(memory)
    await session.commit()
    return True


async def delete_all_memories(session: AsyncSession, learner_id: uuid.UUID) -> int:
    """Erase every memory for ``learner_id`` — the bulk "forget me" endpoint."""
    result = await session.execute(delete(Memory).where(Memory.learner_id == learner_id))
    await session.commit()
    return cast("CursorResult[Any]", result).rowcount
