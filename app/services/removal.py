"""Archive, delete and forget, for sources and conversations (S61, S42; V11).

Archive is out of the way and out of use, and reversible: nothing is deleted. Delete is final
and says first what it keeps. Forget removes what was *derived* from the thing — lessons built
on a source's passages, memories drawn from a conversation — and never the learner's answers
or mastery, which are evidence of what they can do whatever material it came through.

What derives from what is decided in the provenance resolvers below and nowhere else.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation
from app.models.source import Source
from app.rag import pipeline
from app.services import chat as chat_svc


class RemovalRefused(Exception):
    """The request is valid but cannot be carried out now; ``code`` is stable for clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _own_source(
    session: AsyncSession, learner_id: uuid.UUID, source_id: uuid.UUID
) -> Source | None:
    source = await session.get(Source, source_id, populate_existing=True)
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
