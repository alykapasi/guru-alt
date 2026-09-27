"""Archive, delete and forget, for sources and conversations (S61, S42; V11).

Archive is out of the way and out of use, and reversible: nothing is deleted. Delete is final
and says first what it keeps. Forget removes what was *derived* from the thing — lessons built
on a source's passages, memories drawn from a conversation — and never the learner's answers
or mastery, which are evidence of what they can do whatever material it came through.

What derives from what is decided in the provenance resolvers below and nowhere else.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, Message
from app.models.content import ContentBlock
from app.models.erasure import ErasureKind
from app.models.memory import ForgetScope, Memory, MemoryStatus
from app.models.profile import LearnerProfile
from app.models.source import Source, SourceStatus
from app.rag import pipeline
from app.services import chat as chat_svc
from app.services import ingestion, retention
from app.storage import BlobStore

log = structlog.get_logger()


class RemovalRefused(Exception):
    """The request is valid but cannot be carried out now; ``code`` is stable for clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _own_source(
    session: AsyncSession, learner_id: uuid.UUID, source_id: uuid.UUID, *, lock: bool = False
) -> Source | None:
    source = await session.get(
        Source, source_id, populate_existing=True, with_for_update=lock or None
    )
    return source if source is not None and source.learner_id == learner_id else None


async def set_source_archived(
    session: AsyncSession, learner_id: uuid.UUID, source_id: uuid.UUID, *, archived: bool
) -> Source | None:
    """Archive or unarchive a source. ``None`` if it is not this learner's.

    Unarchiving re-deduplicates: while it was away a same-text source may have started
    answering in the same scope (its duplicate, released by the sweep), and two answering
    copies crowd each other out of every grounding window. The stored text digest decides, so
    no extraction runs.
    """
    source = await _own_source(session, learner_id, source_id)
    if source is None:
        return None
    source.archived_at = datetime.now(UTC) if archived else None
    if not archived:
        await session.flush()
        twin = await pipeline.same_text_source(session, source)
        if twin is not None:
            source.duplicate_of_id = twin.id
            await pipeline.supersede_chunks(session, source)
    await session.commit()
    await session.refresh(source)
    return source


async def set_conversation_archived(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID, *, archived: bool
) -> Conversation | None:
    """Archive or unarchive a conversation: out of the list and read-only, memories untouched."""
    conversation = await chat_svc.get_conversation(session, conversation_id, learner_id=learner_id)
    if conversation is None or conversation.learner_id != learner_id:
        return None
    conversation.archived_at = datetime.now(UTC) if archived else None
    await session.commit()
    return await chat_svc.get_conversation(session, conversation_id, learner_id=learner_id)


_PROGRESS_NOTE = "Your answers and progress stay — they are evidence of what you can do."


@dataclass(frozen=True)
class Impact:
    """What removing something keeps, and what "also forget" would take with it."""

    kept: dict[str, int]
    forgettable: dict[str, int]
    notes: list[str]


# --- provenance: what derives from what (the only place this is decided) --------------------


def _lessons_citing(learner_id: uuid.UUID, source_id: uuid.UUID):
    """Lessons and other content blocks built on this source's passages."""
    return select(ContentBlock.id).where(
        ContentBlock.learner_id == learner_id,
        ContentBlock.citations.contains([{"source_id": str(source_id)}]),
    )


def _replies_citing(learner_id: uuid.UUID, source_id: uuid.UUID):
    """Chat replies that cite it — kept always; they show the passage as no longer available."""
    return (
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.learner_id == learner_id,
            Message.citations.contains([{"source_id": str(source_id)}]),
        )
    )


def _memories_from(learner_id: uuid.UUID, conversation_id: uuid.UUID):
    """Current memories learned in this conversation, live, archived or deleted."""
    return select(Memory.id).where(
        Memory.learner_id == learner_id,
        Memory.origin_conversation_id == conversation_id,
        Memory.status == MemoryStatus.CURRENT,
    )


async def _count(session: AsyncSession, stmt) -> int:
    return (await session.scalar(select(func.count()).select_from(stmt.subquery()))) or 0


# --- impact -----------------------------------------------------------------------------------


async def _source_impact(session: AsyncSession, source: Source) -> Impact:
    lessons = await _count(session, _lessons_citing(source.learner_id, source.id))
    replies = await _count(session, _replies_citing(source.learner_id, source.id))
    notes = [_PROGRESS_NOTE]
    if replies:
        notes.append("Replies that cited this file will show it as no longer available.")
    if source.subject_id is not None:
        notes.append("The subject this file belongs to stays.")
    return Impact(
        kept={"lessons": lessons, "cited_replies": replies},
        forgettable={"lessons": lessons},
        notes=notes,
    )


async def _conversation_impact(session: AsyncSession, conversation: Conversation) -> Impact:
    memories = await _count(session, _memories_from(conversation.learner_id, conversation.id))
    notes = [_PROGRESS_NOTE]
    if memories:
        notes.append(
            "Memories from this conversation stay unless you forget them; you can also forget "
            "them later from Memory."
        )
    return Impact(kept={"memories": memories}, forgettable={"memories": memories}, notes=notes)


async def source_impact(
    session: AsyncSession, learner_id: uuid.UUID, source_id: uuid.UUID
) -> Impact | None:
    source = await _own_source(session, learner_id, source_id)
    return None if source is None else await _source_impact(session, source)


async def conversation_impact(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID
) -> Impact | None:
    conversation = await _own_conversation(session, learner_id, conversation_id)
    return None if conversation is None else await _conversation_impact(session, conversation)


async def _own_conversation(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation | None:
    conversation = await session.get(Conversation, conversation_id, populate_existing=True)
    return (
        conversation if conversation is not None and conversation.learner_id == learner_id else None
    )


async def _clear_profile_watermark(session: AsyncSession, learner_id: uuid.UUID) -> None:
    """The next profile refresh recomputes from what remains instead of reporting no change."""
    await session.execute(
        update(LearnerProfile)
        .where(LearnerProfile.learner_id == learner_id)
        .values(evidence_watermark=None)
    )


# --- delete -----------------------------------------------------------------------------------


async def delete_source(
    session: AsyncSession,
    blobstore: BlobStore,
    learner_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    forget: bool,
) -> Impact | None:
    """Delete a source now. Returns what was kept (and, with ``forget``, what went).

    Chunks — cited history too — KC tags and conversation links cascade; a duplicate's link
    nulls and the recovery sweep releases it (S77). The file goes after the commit, and only
    when no other source shares it; a store failure is reported, not raised — the database is
    already consistent, and an orphaned file is recoverable where a half-deleted source is not.
    """
    # Locked until the delete commits: a worker's claim is an UPDATE of this row, so it waits
    # and then matches nothing, rather than paying for an extraction whose chunks cannot land.
    source = await _own_source(session, learner_id, source_id, lock=True)
    if source is None:
        return None
    if source.status == SourceStatus.PROCESSING and source.lease_expires_at is not None:
        if await session.scalar(select(func.now() < source.lease_expires_at)):
            raise RemovalRefused("ingesting", "This source is being processed right now.")
    impact = await _source_impact(session, source)
    blob_key = source.blob_key
    if forget:
        await session.execute(
            delete(ContentBlock).where(ContentBlock.id.in_(_lessons_citing(learner_id, source_id)))
        )
        await _clear_profile_watermark(session, learner_id)
    await session.execute(delete(Source).where(Source.id == source_id))
    await session.commit()
    notes = list(impact.notes)
    if blob_key is not None:
        try:
            await ingestion.unreference_blob(session, blobstore, blob_key)
        except Exception:
            log.warning("removal.blob_not_deleted", source_id=str(source_id), exc_info=True)
            await retention.queue_erasure(
                session, ErasureKind.BLOB, blob_key, "refused at source delete"
            )
            notes.append("The stored file could not be removed yet; it will be retried.")
    return _after(impact, forget=forget, notes=notes)


def _after(impact: Impact, *, forget: bool, notes: list[str]) -> Impact:
    """What a finished delete reports: with ``forget``, nothing it removed is counted as kept."""
    kept = {k: 0 if forget and k in impact.forgettable else v for k, v in impact.kept.items()}
    return Impact(kept=kept, forgettable=impact.forgettable, notes=notes)


async def delete_conversation(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID, *, forget: bool
) -> Impact | None:
    """Delete a conversation now. With ``forget``, the memories it taught are forgotten too.

    Messages and turns cascade; ``LLMCall.conversation_id`` is SET NULL, so the cost log
    survives by design; memories keep ``origin_conversation_id`` for a later forget.
    """
    conversation = await _own_conversation(session, learner_id, conversation_id)
    if conversation is None:
        return None
    impact = await _conversation_impact(session, conversation)
    if forget:
        await session.execute(
            update(Memory)
            .where(Memory.id.in_(_memories_from(learner_id, conversation_id)))
            .values(status=MemoryStatus.DELETED, forgotten_scope=ForgetScope.CONVERSATION)
        )
        await _clear_profile_watermark(session, learner_id)
    await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await session.commit()
    return _after(impact, forget=forget, notes=impact.notes)


async def forget_conversation_memories(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID
) -> int:
    """Forget what a conversation taught — live, archived or already deleted (S42, V11).

    The existing soft delete, so re-extraction recognises each fact and does not bring it back.
    Memories the learner wrote themselves have no origin and are never touched. Idempotent: a
    second call finds nothing current and returns 0.
    """
    ids = list((await session.scalars(_memories_from(learner_id, conversation_id))).all())
    if ids:
        await session.execute(
            update(Memory)
            .where(Memory.id.in_(ids))
            .values(status=MemoryStatus.DELETED, forgotten_scope=ForgetScope.CONVERSATION)
        )
        await _clear_profile_watermark(session, learner_id)
    await session.commit()
    return len(ids)
