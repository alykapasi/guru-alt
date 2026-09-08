"""Hybrid retrieval: pgvector cosine + Postgres full-text, fused by RRF (§6.2).

Two ranked candidate lists — semantic (HNSW cosine over the query embedding) and lexical
(``tsvector`` keyword) — are merged with **reciprocal-rank fusion**, then the top hits are
returned with their provenance for grounded, citable generation. Every query is **scoped**
(learner always; optionally subject/topic/source) via the chunk → source join.
"""

import uuid
from collections.abc import Sequence

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ModelRole
from app.llm.embedding_space import current_space
from app.models.source import Chunk, Source

_RRF_K = 60  # standard reciprocal-rank-fusion constant


class RetrievalHit(BaseModel):
    """A retrieved chunk with its fused score and provenance."""

    chunk_id: uuid.UUID
    source_id: uuid.UUID
    text: str
    provenance: dict
    score: float


async def retrieve(
    session: AsyncSession,
    llm: LLMClient,
    query: str,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None = None,
    topic_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    source_ids: Sequence[uuid.UUID] | None = None,
    limit: int = 10,
    candidates: int = 50,
) -> list[RetrievalHit]:
    """Hybrid-retrieve the most relevant chunks for ``query`` within the given scope.

    ``source_ids`` narrows to several specific sources (a conversation's explicit picks, see
    ``ConversationSource``) — distinct from ``source_id``, which narrows to exactly one (the
    debug ``/retrieve`` endpoint's existing use). Both may be combined with ``subject_id``.
    """
    query = query.strip()
    if not query:
        return []

    space = current_space(llm, dim=get_settings().embed_dim)

    def scoped(stmt: Select) -> Select:
        stmt = stmt.join(Source, Chunk.source_id == Source.id).where(
            Source.learner_id == learner_id
        )
        if source_id is not None:
            stmt = stmt.where(Chunk.source_id == source_id)
        if source_ids is not None:
            stmt = stmt.where(Chunk.source_id.in_(source_ids))
        if subject_id is not None:
            stmt = stmt.where(Source.subject_id == subject_id)
        if topic_id is not None:
            stmt = stmt.where(Source.topic_id == topic_id)
        return stmt

    query_vec = (await llm.embed(ModelRole.EMBED, [query])).vectors[0]
    tsquery = func.plainto_tsquery("english", query)

    # Only vectors from the query's own space are comparable to it. Chunks embedded by a
    # previous model are excluded rather than ranked — a wrong answer that looks right is
    # worse than a missing one, and the keyword arm still reaches them.
    vector_q = (
        scoped(select(Chunk))
        .where(Chunk.embedding_space == space)
        .order_by(Chunk.embedding.cosine_distance(query_vec))
        .limit(candidates)
    )
    keyword_q = (
        scoped(select(Chunk))
        .where(Chunk.tsv.op("@@")(tsquery))
        .order_by(func.ts_rank(Chunk.tsv, tsquery).desc())
        .limit(candidates)
    )

    vector_hits = list((await session.scalars(vector_q)).all())
    keyword_hits = list((await session.scalars(keyword_q)).all())

    by_id = {chunk.id: chunk for chunk in (*vector_hits, *keyword_hits)}
    scores = _fuse([c.id for c in vector_hits], [c.id for c in keyword_hits])
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [
        RetrievalHit(
            chunk_id=chunk_id,
            source_id=by_id[chunk_id].source_id,
            text=by_id[chunk_id].text,
            provenance=by_id[chunk_id].provenance,
            score=score,
        )
        for chunk_id, score in ranked
    ]


def _fuse(*ranked_lists: list[uuid.UUID], k: int = _RRF_K) -> dict[uuid.UUID, float]:
    """Reciprocal-rank fusion: a chunk's score sums 1/(k + position) across the lists."""
    scores: dict[uuid.UUID, float] = {}
    for ids in ranked_lists:
        for position, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position)
    return scores
