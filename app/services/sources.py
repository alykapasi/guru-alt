"""Reading a learner's source library, a page at a time (S62).

The list used to return every source the learner had ever uploaded, in one query whose rows
grew with their library. It is a page now, keyed like the transcript and the conversation list
on ``(created_at, id)`` so uploads made in one transaction are neither skipped nor repeated.
"""

import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source import Source


async def list_sources(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    subject_id: uuid.UUID | None = None,
    archived: bool = False,
    limit: int,
    before: uuid.UUID | None = None,
) -> tuple[list[Source], bool]:
    """One page, newest first; ``(sources, has_more)``. A stale or foreign cursor yields the
    first page."""
    stmt = select(Source).where(
        Source.learner_id == learner_id,
        Source.archived_at.is_not(None) if archived else Source.archived_at.is_(None),
    )
    if subject_id is not None:
        stmt = stmt.where(Source.subject_id == subject_id)
    if before is not None:
        anchor = await session.get(Source, before)
        if anchor is not None and anchor.learner_id == learner_id:
            stmt = stmt.where(tuple_(Source.created_at, Source.id) < (anchor.created_at, anchor.id))
    rows = list(
        (
            await session.scalars(
                stmt.order_by(Source.created_at.desc(), Source.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    return rows[:limit], len(rows) > limit
