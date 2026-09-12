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

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ModelRole
from app.llm.embedding_space import current_space
from app.memory.extraction import EXTRACTION_ROLE, extract_memories
from app.models.chat import Conversation, Message
from app.models.memory import Memory, MemoryKind, MemoryStatus
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
        # Two separate questions, deliberately not one nearest-row lookup. Superseded rows are
        # excluded from both: one has already been replaced, so its replacement is the thing
        # this should be compared against, and a superseded row that happens to sit closer
        # would otherwise be answered for.
        tombstone = await _nearest(
            session,
            conversation.learner_id,
            item.kind,
            embedding,
            max_distance=settings.memory_dedup_max_distance,
            space=space,
            status=MemoryStatus.DELETED,
        )
        if tombstone is not None:
            # The learner removed this. Re-extracting it from the same history is how a
            # deleted memory used to come back; the tombstone is what stops that.
            continue
        prior = await _nearest(
            session,
            conversation.learner_id,
            item.kind,
            embedding,
            max_distance=settings.memory_dedup_max_distance,
            space=space,
            status=MemoryStatus.CURRENT,
        )
        if prior is not None and prior.content.strip() == item.content.strip():
            continue  # genuinely nothing new
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
        if prior is not None:
            # Close but not identical: the learner said something that revises this. Skipping
            # kept the *stale* entry and discarded the correction — precisely backwards. The
            # link makes it a correction on the record rather than a silent overwrite.
            await session.flush()
            prior.status = MemoryStatus.SUPERSEDED
            prior.superseded_by_id = memory.id
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


async def _nearest(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kind: MemoryKind,
    embedding: list[float],
    *,
    max_distance: float,
    space: str,
    status: MemoryStatus,
) -> Memory | None:
    """The learner's closest memory of ``kind`` in ``status``, if it is close enough.

    Returns the row rather than a boolean because what to do depends on which row it is: a
    deleted one means suppress, a current one with different wording means supersede, an
    identical one means skip. Collapsing all three to "duplicate, skip" is what discarded
    corrections and let deleted memories return.

    Measured only against its own embedding space: a distance to a vector from another model
    is not a distance to anything, and would both miss real duplicates and suppress genuinely
    new memories at random.
    """
    distance = Memory.embedding.cosine_distance(embedding)
    row = (
        await session.execute(
            select(Memory, distance.label("distance"))
            .where(
                Memory.learner_id == learner_id,
                Memory.kind == kind,
                Memory.embedding_space == space,
                Memory.status == status,
            )
            .order_by(distance)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    memory, dist = row
    return memory if dist <= max_distance else None


async def list_memories(
    session: AsyncSession, learner_id: uuid.UUID, *, limit: int = 50
) -> Sequence[Memory]:
    """What the learner would recognise as their memory: current entries only.

    Superseded and deleted rows exist to make extraction well-behaved, not to be shown back.
    """
    result = await session.scalars(
        select(Memory)
        .where(Memory.learner_id == learner_id, Memory.status == MemoryStatus.CURRENT)
        .order_by(Memory.created_at.desc())
        .limit(limit)
    )
    return result.all()


async def correct_memory(
    session: AsyncSession,
    llm: LLMClient,
    learner_id: uuid.UUID,
    memory_id: uuid.UUID,
    *,
    content: str,
) -> Memory | None:
    """Replace what is remembered about a learner with what they say is true (S16).

    Erasure was the only correction available, and it is the wrong tool for the common case:
    most of what goes wrong with an extracted memory is that it is *nearly* right — the right
    subject, the wrong detail. Deleting it loses the true part, and leaves the extractor free
    to derive the same wrong thing again from the same history, because a tombstone only
    suppresses a fact, it does not assert the correction.

    So a correction supersedes rather than overwrites, exactly as an extracted contradiction
    already does: the old row keeps its text and its embedding and gains a pointer to the new
    one. Three things follow from that, and each is the reason not to do the simpler thing:

    * The old embedding still recognises the old claim, so a later write-back over the same
      conversation finds the superseded row's *replacement* to compare against rather than
      re-creating the mistake.
    * The correction is visible as a correction. Overwriting ``content`` in place would make
      the system's belief look like it had always been what the learner just typed, which is
      the one thing a record of what somebody was told must never do.
    * A learner asking "why does it think that?" can be answered, because the chain is intact.

    The new text is embedded rather than inheriting the old vector. A corrected memory with its
    predecessor's embedding is retrieved for the wrong questions and missed for the right ones,
    which is a subtle version of not having corrected it at all.

    Returns the new memory, or ``None`` when the id is not this learner's, is already deleted,
    or the text is unchanged.
    """
    memory = await session.get(Memory, memory_id)
    if memory is None or memory.learner_id != learner_id:
        return None
    if memory.status != MemoryStatus.CURRENT:
        return None  # a deleted or already-superseded row is not the one they are looking at
    corrected = content.strip()
    if not corrected or corrected == memory.content.strip():
        return None

    settings = get_settings()
    space = current_space(llm, dim=settings.embed_dim)
    embedded = await llm.embed(ModelRole.EMBED, [corrected])
    if embedded.usage.total_tokens:
        await log_llm_call(
            learner_id=learner_id,
            role=str(ModelRole.EMBED),
            spec=llm.spec(ModelRole.EMBED),
            usage=embedded.usage,
        )
    replacement = Memory(
        learner_id=learner_id,
        # Provenance follows the correction: this came from the learner saying so, not from
        # the conversation the original was extracted from.
        conversation_id=None,
        kind=memory.kind,
        content=corrected,
        embedding=embedded.vectors[0],
        embedding_space=space,
    )
    session.add(replacement)
    await session.flush()
    memory.status = MemoryStatus.SUPERSEDED
    memory.superseded_by_id = replacement.id
    await session.commit()
    await session.refresh(replacement)
    return replacement


async def delete_memory(session: AsyncSession, learner_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
    """Forget one memory the learner owns. ``False`` (no-op) if missing or not theirs.

    Soft, and that is the point: the row's embedding is the only thing that can recognise the
    same fact being extracted again from the same history. Hard-deleting it is what let a
    deleted memory reappear. It stops being retrievable immediately either way.
    """
    memory = await session.get(Memory, memory_id)
    if memory is None or memory.learner_id != learner_id:
        return False
    if memory.status == MemoryStatus.DELETED:
        return False
    memory.status = MemoryStatus.DELETED
    await session.commit()
    return True


async def delete_all_memories(session: AsyncSession, learner_id: uuid.UUID) -> int:
    """Forget every memory for ``learner_id`` — the bulk "forget me" endpoint.

    Soft, for the same reason as the single-row case: these have to stay recognisable or the
    next write-back over the same conversations rebuilds what was just erased.

    A learner who wants the rows *gone* rather than forgotten is asking a different question —
    data erasure, which has to cover chat history and events too, and belongs with S61's
    retention policy rather than here.
    """
    result = await session.execute(
        update(Memory)
        .where(Memory.learner_id == learner_id, Memory.status != MemoryStatus.DELETED)
        .values(status=MemoryStatus.DELETED)
    )
    await session.commit()
    return cast("CursorResult[Any]", result).rowcount
