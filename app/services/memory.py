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

from sqlalchemy import CursorResult, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ModelRole
from app.llm.attribution import bind, metered
from app.llm.embedding_space import current_space, exact_cosine_distance
from app.memory import supersession
from app.memory.extraction import ExtractedMemory, extract_memories
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.memory import ForgetScope, Memory, MemoryKind, MemoryStatus
from app.services.turn_common import to_chat_messages


@metered("memory_write_back", conversation="conversation_id")
async def write_back(
    session: AsyncSession, llm: LLMClient, *, conversation_id: uuid.UUID
) -> list[Memory]:
    """Extract durable memories from ``conversation_id``'s recent history and persist them.

    Unknown/foreign ``conversation_id`` -> ``[]``, no crash (the queued task may run after the
    conversation's owner state has changed). A candidate identical to an existing
    same-``(learner_id, kind)`` memory within ``memory_dedup_max_distance`` is skipped for free.
    One with current neighbours within ``memory_related_max_distance`` is judged same / updates /
    coexists in one FAST call per run (S42); doubt means coexists, so nothing true is retired on
    a guess. Nothing is learned while the learner has memory paused (S43).
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return []
    # The calls below are this learner's (S48); restored when ``metered`` exits.
    bind(learner_id=conversation.learner_id)
    learner = await session.get(Learner, conversation.learner_id)
    if (
        learner is None
        or not learner.remember_conversations
        or learner.deletion_requested_at is not None
        or learner.suspended_at is not None
    ):
        # Paused (S43), or the account closed or was suspended after this was queued: no model
        # call and nothing learned. What is already remembered is untouched.
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
        conversation.memory_attempted_at = None
        await session.commit()
        return []
    # Advance to what this run actually read, not to "now": a message written while extraction
    # is in flight must still be picked up by the next run rather than stepped over.
    conversation.memory_watermark = window[-1].created_at
    # Done with this claim (S43): a backlog bigger than the window stays due and is picked up
    # on the next pass, not after the delay meant for failures.
    conversation.memory_attempted_at = None
    extracted, _usage = await extract_memories(llm, to_chat_messages(window))
    if not extracted:
        await session.commit()
        return []

    space = current_space(llm, dim=settings.embed_dim)
    embedded = await llm.embed(ModelRole.EMBED, [item.content for item in extracted])
    created: list[Memory] = []
    # Candidates the judge must decide: (item, embedding, neighbour rows).
    pending: list[tuple[ExtractedMemory, list[float], list[Memory]]] = []
    for item, embedding in zip(extracted, embedded.vectors, strict=True):
        if await _suppressed(
            session,
            conversation.learner_id,
            item.kind,
            embedding,
            conversation_id=conversation_id,
            max_distance=settings.memory_dedup_max_distance,
            space=space,
        ):
            # The learner forgot this. Re-extracting it is how a forgotten memory used to come
            # back; the tombstone is what stops that (scoped, since S42 decision B).
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
            continue  # genuinely nothing new — no need to ask anyone
        neighbours = (
            []
            if item.kind == MemoryKind.SUMMARY  # what was covered always coexists
            else await _neighbours(
                session,
                conversation.learner_id,
                item.kind,
                embedding,
                max_distance=settings.memory_related_max_distance,
                space=space,
            )
        )
        if not neighbours:
            # Added now, so a later item in this batch sees it (autoflush makes it visible to
            # the next query) — intra-batch dedup, deliberately not batched.
            created.append(_new_memory(conversation, item, embedding, space))
            session.add(created[-1])
            continue
        pending.append((item, embedding, neighbours))

    if pending:
        judgements, _usage = await supersession.judge(
            llm,
            [
                supersession.Candidate(item.content, [n.content for n in neighbours])
                for item, _embedding, neighbours in pending
            ],
        )
        for (item, embedding, neighbours), judgement in zip(pending, judgements, strict=True):
            if judgement.verdict == "same":
                continue
            memory = _new_memory(conversation, item, embedding, space)
            session.add(memory)
            created.append(memory)
            if judgement.verdict == "updates" and judgement.target is not None:
                # Two candidates can name the same neighbour, and the learner can forget or
                # correct it while the judge is thinking: only a still-current row is replaced.
                # Otherwise the new memory simply coexists.
                await session.flush()
                await _supersede(session, neighbours[judgement.target].id, memory.id)
    await session.commit()
    return created


def _new_memory(
    conversation: Conversation, item: ExtractedMemory, embedding: list[float], space: str
) -> Memory:
    return Memory(
        embedding_space=space,
        learner_id=conversation.learner_id,
        conversation_id=conversation.id,
        origin_conversation_id=conversation.id,
        kind=item.kind,
        content=item.content,
        embedding=embedding,
    )


async def _neighbours(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kind: MemoryKind,
    embedding: list[float],
    *,
    max_distance: float,
    space: str,
    limit: int = 3,
) -> list[Memory]:
    """The learner's current memories of ``kind`` close enough to be about the same thing,
    nearest first — what the judge compares a candidate against."""
    distance = exact_cosine_distance(Memory.embedding, embedding)
    rows = (
        await session.execute(
            select(Memory, distance.label("distance"))
            .where(
                Memory.learner_id == learner_id,
                Memory.kind == kind,
                Memory.embedding_space == space,
                Memory.status == MemoryStatus.CURRENT,
            )
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [memory for memory, dist in rows if dist <= max_distance]


async def _suppressed(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kind: MemoryKind,
    embedding: list[float],
    *,
    conversation_id: uuid.UUID,
    max_distance: float,
    space: str,
) -> bool:
    """Whether a forgotten memory stops this one being stored.

    A learner-wide tombstone suppresses everywhere; a conversation-scoped one only re-extraction
    from the conversation it was learned in (decision B).
    """
    distance = exact_cosine_distance(Memory.embedding, embedding)
    row = (
        await session.execute(
            select(distance.label("distance"))
            .where(
                Memory.learner_id == learner_id,
                Memory.kind == kind,
                Memory.embedding_space == space,
                Memory.status == MemoryStatus.DELETED,
                or_(
                    Memory.forgotten_scope.is_(None),  # defensive: pre-0069 shape
                    Memory.forgotten_scope == ForgetScope.LEARNER,
                    and_(
                        Memory.forgotten_scope == ForgetScope.CONVERSATION,
                        Memory.origin_conversation_id == conversation_id,
                    ),
                ),
            )
            .order_by(distance)
            .limit(1)
        )
    ).first()
    return row is not None and row[0] <= max_distance


class NotAReplacement(Exception):
    """Undo asked of a memory that replaced nothing still restorable."""


async def replaced_by(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Memory]:
    """For each id, the superseded memory it most recently replaced (if any)."""
    if not ids:
        return {}
    rows = (
        await session.scalars(
            select(Memory)
            .where(Memory.superseded_by_id.in_(ids), Memory.status == MemoryStatus.SUPERSEDED)
            .order_by(Memory.created_at.desc())
        )
    ).all()
    found: dict[uuid.UUID, Memory] = {}
    for row in rows:
        assert row.superseded_by_id is not None
        found.setdefault(row.superseded_by_id, row)
    return found


async def undo_replacement(
    session: AsyncSession, learner_id: uuid.UUID, memory_id: uuid.UUID
) -> Memory | None:
    """Put back what ``memory_id`` replaced; retire ``memory_id`` without forgetting it (S42).

    ``None`` when the id is not this learner's. ``NotAReplacement`` when it is not current, or
    nothing it replaced is still superseded by it — the old row was since corrected, forgotten
    or already restored. Nothing is suppressed: the retired statement, said again, is judged
    afresh.
    """
    memory = await session.get(Memory, memory_id, with_for_update=True)
    if memory is None or memory.learner_id != learner_id:
        return None
    if memory.status != MemoryStatus.CURRENT:
        raise NotAReplacement(str(memory_id))
    old = (await replaced_by(session, [memory_id])).get(memory_id)
    if old is None:
        raise NotAReplacement(str(memory_id))
    old.status = MemoryStatus.CURRENT
    old.superseded_by_id = None
    await session.flush()
    memory.status = MemoryStatus.SUPERSEDED
    memory.superseded_by_id = old.id
    await session.commit()
    await session.refresh(old)
    return old


async def set_remember(session: AsyncSession, learner_id: uuid.UUID, remember: bool) -> bool:
    """Pause or resume memory for a learner (S43); returns the setting now in force.

    Resuming moves every conversation's cursor to its newest message, so what was said while
    memory was paused is never learned from — turning it back on is not consent to go back and
    read the pause.
    """
    learner = await session.get(Learner, learner_id)
    assert learner is not None
    if remember and not learner.remember_conversations:
        newest = dict(
            (
                await session.execute(
                    select(Message.conversation_id, func.max(Message.created_at))
                    .join(Conversation, Message.conversation_id == Conversation.id)
                    .where(Conversation.learner_id == learner_id)
                    .group_by(Message.conversation_id)
                )
            )
            .tuples()
            .all()
        )
        conversations = await session.scalars(
            select(Conversation).where(Conversation.id.in_(newest))
        )
        for conversation in conversations.all():
            conversation.memory_watermark = newest[conversation.id]
    learner.remember_conversations = remember
    await session.commit()
    await session.refresh(learner)  # `updated_at` is set by the database
    return learner.remember_conversations


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
    stmt = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.admin_actor_id.is_(None),
        Message.admin_action_id.is_(None),
    )
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
    distance = exact_cosine_distance(Memory.embedding, embedding)
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


@metered("memory_correction", learner="learner_id")
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
    if not await _supersede(session, memory.id, replacement.id):
        # Forgotten or replaced while the correction was being embedded.
        await session.rollback()
        return None
    await session.commit()
    await session.refresh(replacement)
    return replacement


async def _supersede(session: AsyncSession, old_id: uuid.UUID, new_id: uuid.UUID) -> bool:
    """Mark ``old_id`` replaced by ``new_id`` — only if it is still current.

    Conditional in the database, not on a copy read before a model call: a Forget that landed
    meanwhile must stay a Forget, not become a replacement that Undo could bring back.
    """
    result = await session.execute(
        update(Memory)
        .where(Memory.id == old_id, Memory.status == MemoryStatus.CURRENT)
        .values(status=MemoryStatus.SUPERSEDED, superseded_by_id=new_id)
        .execution_options(synchronize_session="fetch")
    )
    return cast("CursorResult[Any]", result).rowcount == 1


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
    memory.forgotten_scope = ForgetScope.LEARNER
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
        .values(status=MemoryStatus.DELETED, forgotten_scope=ForgetScope.LEARNER)
    )
    # What was forgotten with one conversation is now forgotten everywhere too.
    await session.execute(
        update(Memory)
        .where(
            Memory.learner_id == learner_id,
            Memory.status == MemoryStatus.DELETED,
            Memory.forgotten_scope != ForgetScope.LEARNER,
        )
        .values(forgotten_scope=ForgetScope.LEARNER)
    )
    await session.commit()
    return cast("CursorResult[Any]", result).rowcount
