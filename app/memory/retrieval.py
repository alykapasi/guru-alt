"""Vector-only memory retrieval (§7.1's schema sketch has no ``tsv`` column, unlike ``Chunk`` —
short factual content doesn't need hybrid keyword fusion for v1; see ``app/rag/retrieval.py``
for the fuller hybrid pattern this would extend into if that changes).

Every query is scoped to one learner — memory is learner-global, not subject/topic-scoped
(matches how the learner profile is also learner-global).
"""

import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ModelRole
from app.llm.embedding_space import current_space
from app.models.memory import Memory


class MemoryHit(BaseModel):
    """A retrieved memory with its similarity score (1.0 - cosine distance)."""

    id: uuid.UUID
    kind: str
    content: str
    score: float


async def retrieve(
    session: AsyncSession, llm: LLMClient, query: str, *, learner_id: uuid.UUID, limit: int = 5
) -> list[MemoryHit]:
    """The most relevant memories for ``query`` within ``learner_id``'s memory."""
    query = query.strip()
    if not query:
        return []

    query_vec = (await llm.embed(ModelRole.EMBED, [query])).vectors[0]
    distance = Memory.embedding.cosine_distance(query_vec)
    rows = (
        await session.execute(
            select(Memory, distance.label("distance"))
            .where(
                Memory.learner_id == learner_id,
                # Memories embedded by a previous model are not comparable to this query.
                Memory.embedding_space == current_space(llm, dim=get_settings().embed_dim),
            )
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [
        MemoryHit(id=memory.id, kind=memory.kind, content=memory.content, score=1.0 - dist)
        for memory, dist in rows
    ]
