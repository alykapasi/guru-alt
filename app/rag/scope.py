"""What a piece of generation may draw on (S26, V05) — decided once, here.

Every grounding path used to assemble its own filter: lessons admitted untagged sources, chat
did not, and the agent's search in a "General — no library grounding" chat searched the whole
library. ``resolve_scope`` is now the only place those decisions are made, so the paths cannot
drift apart again.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject


@dataclass(frozen=True)
class SourceScope:
    """The sources one retrieval may read. Neither a subject nor sources means the learner's
    whole library, which only the debug ``/retrieve`` endpoint asks for."""

    learner_id: uuid.UUID
    subject_id: uuid.UUID | None = None
    topic_id: uuid.UUID | None = None
    source_ids: tuple[uuid.UUID, ...] = ()
    include_untagged: bool = False
    sources_only: bool = False


async def resolve_scope(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    source_ids: Sequence[uuid.UUID] = (),
) -> SourceScope | None:
    """The scope for a conversation or lesson, or ``None`` for no library at all.

    ``None`` is a General conversation, and callers skip retrieval rather than asking for an
    empty result. Picked sources are the learner's explicit choice, so they turn the untagged
    switch off: they already said which material this is.
    """
    picked = tuple(source_ids)
    if subject_id is None:
        return SourceScope(learner_id=learner_id, source_ids=picked) if picked else None
    subject = await session.get(Subject, subject_id)
    if subject is None:
        raise LookupError(f"subject {subject_id} not found")
    return SourceScope(
        learner_id=learner_id,
        subject_id=subject_id,
        source_ids=picked,
        include_untagged=subject.include_untagged_sources and not picked,
        sources_only=subject.sources_only,
    )
