"""Per-learner memory: write-back (extract + dedup + persist) and view/erase.

``write_back`` is triggered on-demand (``POST /conversations/{id}/memory/write-back``, queued —
see ``app.workers.tasks.memory_write_back_task``), not automatically on every turn: extraction
costs one FAST call, and firing it per-message would pay that repeatedly for no accumulated
benefit (same reasoning already applied to ``POST /profile/refresh``).

It is **incremental** (S43): each conversation carries a ``memory_watermark`` naming the newest
message already extracted from, and a run reads the *oldest* unprocessed messages forward from
there. Before this it read the most recent N messages regardless, so a conversation that grew
by more than N between runs had the middle skipped entirely, and one that grew by nothing paid
a model call to rediscover it had nothing to do. Retrieval
(``app.memory.retrieval``) is what actually "wires memory into sessions" — it runs on every
tutor turn instead, since it's cheap (one EMBED call + one DB query, same tier RAG retrieval
already pays).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ModelRole
from app.llm.embedding_space import current_space
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
    window = await _unprocessed_messages(
        session,
        conversation_id,
        after=conversation.memory_watermark,
        limit=settings.memory_extraction_window,
    )
    if not window:
        # Nothing new since the last run. Returning here is the difference between a repeated
        # write-back being free and it costing a FAST call to rediscover it had nothing to do.
        return []
    # Advance to what this run actually read, not to "now": a message written while extraction
    # is in flight must still be picked up by the next run rather than stepped over.
    conversation.memory_watermark = window[-1].created_at
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

    space = current_space(llm, dim=settings.embed_dim)
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
            space=space,
        ):
            continue
        memory = Memory(
            embedding_space=space,
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


async def _unprocessed_messages(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    after: datetime | None,
    limit: int,
) -> list[Message]:
    """The oldest messages this conversation has not been extracted from yet, up to ``limit``.

    Oldest-first rather than newest-first, which is the whole fix: taking the *most recent*
    ``limit`` messages meant a conversation that grew by more than ``limit`` between runs had
    the middle silently dropped — never read, never extracted, gone. Taking the oldest
    unprocessed ones instead leaves a backlog for the next run rather than a hole.

    Ties on ``created_at`` are broken by id, because messages written in one transaction share
    an instant (``server_default=func.now()`` is transaction-start time) and a cursor that
    cannot order within an instant either re-reads or skips.
    """
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if after is not None:
        stmt = stmt.where(Message.created_at > after)
    rows = (await session.scalars(stmt.order_by(Message.created_at, Message.id).limit(limit))).all()
    return list(rows)


async def _is_near_duplicate(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kind: MemoryKind,
    embedding: list[float],
    *,
    max_distance: float,
    space: str,
) -> bool:
    """Whether an equivalent memory already exists — measured only against its own space.

    A distance to a vector from another embedding model is not a distance to anything: it
    would both miss real duplicates and suppress genuinely new memories at random.
    """
    distance = Memory.embedding.cosine_distance(embedding)
    nearest = await session.scalar(
        select(distance)
        .where(
            Memory.learner_id == learner_id,
            Memory.kind == kind,
            Memory.embedding_space == space,
        )
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
