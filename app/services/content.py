"""Content engine: grounded, KC-tagged, cached block generation + assembly (§6.3).

``generate_block`` retrieves grounding chunks for a KC, asks the role-appropriate model to
write the block **citing only those chunks**, and caches the result under a content-addressed
``cache_key`` so an identical request reuses it. ``assemble`` returns the standard set of
blocks for a KC, generating any that are missing. All model access is by role; the service
owns the transaction and logs each call's cost.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.models.content import ContentBlock, ContentType
from app.models.knowledge import KC
from app.rag import retrieval
from app.rag.retrieval import RetrievalHit
from app.services.llm_log import log_llm_call

GROUNDING_K = 6
"""How many chunks to retrieve as grounding for a block."""

DEFAULT_ASSEMBLY: tuple[ContentType, ...] = (ContentType.WIKI_BRIEF, ContentType.LESSON)
"""The block set ``assemble`` materializes for a KC by default."""

_ROLE_BY_TYPE: dict[ContentType, ModelRole] = {
    ContentType.LESSON: ModelRole.SMART,
    ContentType.WIKI_BRIEF: ModelRole.SMART,
    ContentType.WIKI_FULL: ModelRole.GENIUS,  # full synthesis runs on the top tier
    ContentType.QUESTION: ModelRole.SMART,
}

_GUIDANCE: dict[ContentType, str] = {
    ContentType.LESSON: "a clear teaching lesson that explains the concept with worked detail",
    ContentType.WIKI_BRIEF: "a concise reference summary of a few sentences",
    ContentType.WIKI_FULL: "a thorough reference article synthesizing the material",
    ContentType.QUESTION: "a single practice question that checks understanding",
}

_SYSTEM_PROMPT = (
    "You are an expert instructional author. Using ONLY the numbered context snippets, write "
    "{guidance} for the learning objective. Ground every claim in the context and cite the "
    "snippets you used by their index. Respond with ONLY a JSON object of the form "
    '{{"body": "<the content>", "citations": [<indices of snippets used>]}} and nothing else.'
)


class ContentGenerationError(RuntimeError):
    """The model's reply could not be parsed into a content block."""


class _GeneratedBlock(BaseModel):
    body: str
    citations: list[int] = []


async def generate_block(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    kc_id: uuid.UUID,
    block_type: ContentType,
    grounding_k: int = GROUNDING_K,
) -> ContentBlock:
    """Generate (or reuse) a cached block for ``kc_id`` of the given type.

    Retrieves grounding chunks, hashes them into a ``cache_key``, and returns the existing
    block if one is cached for that exact (learner, KC, type, grounding) — otherwise generates,
    persists, logs cost, and commits.
    """
    kc = await session.get(KC, kc_id)
    if kc is None:
        raise LookupError(f"KC {kc_id} not found")

    grounding = await retrieval.retrieve(
        session, llm, _kc_query(kc), learner_id=learner_id, limit=grounding_k
    )
    cache_key = _cache_key(learner_id, [kc_id], block_type, [h.chunk_id for h in grounding])

    existing = await session.scalar(select(ContentBlock).where(ContentBlock.cache_key == cache_key))
    if existing is not None:
        return existing

    role = _ROLE_BY_TYPE[block_type]
    parsed, usage = await _generate(llm, role, kc, block_type, grounding)
    block = ContentBlock(
        learner_id=learner_id,
        kc_ids=[kc_id],
        block_type=block_type,
        body=parsed.body,
        citations=_resolve_citations(parsed.citations, grounding),
        cache_key=cache_key,
        model=llm.spec(role).model,
    )
    session.add(block)
    await log_llm_call(learner_id=learner_id, role=str(role), spec=llm.spec(role), usage=usage)
    await session.commit()
    return block


async def assemble(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    kc_id: uuid.UUID,
    types: Sequence[ContentType] = DEFAULT_ASSEMBLY,
) -> list[ContentBlock]:
    """Return the requested block set for a KC, generating (and caching) any that are missing."""
    return [
        await generate_block(
            session, llm, learner_id=learner_id, kc_id=kc_id, block_type=block_type
        )
        for block_type in types
    ]


async def list_blocks(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    kc_id: uuid.UUID,
    block_type: ContentType | None = None,
) -> list[ContentBlock]:
    """Read cached blocks tagged with ``kc_id`` for a learner (no generation)."""
    stmt = (
        select(ContentBlock)
        .where(ContentBlock.learner_id == learner_id, ContentBlock.kc_ids.contains([kc_id]))
        .order_by(ContentBlock.created_at)
    )
    if block_type is not None:
        stmt = stmt.where(ContentBlock.block_type == block_type)
    return list((await session.scalars(stmt)).all())


def _kc_query(kc: KC) -> str:
    """The retrieval query for a KC — its name plus description."""
    return f"{kc.name}. {kc.description}" if kc.description else kc.name


async def _generate(
    llm: LLMClient,
    role: ModelRole,
    kc: KC,
    block_type: ContentType,
    grounding: list[RetrievalHit],
) -> tuple[_GeneratedBlock, Usage]:
    """Call the model and parse its reply into a body + cited snippet indices."""
    completion = await llm.complete(
        role,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc, grounding))],
        system=_SYSTEM_PROMPT.format(guidance=_GUIDANCE[block_type]),
        max_tokens=1024,
    )
    return _parse(completion.content), completion.usage


def _build_prompt(kc: KC, grounding: list[RetrievalHit]) -> str:
    objective = _kc_query(kc)
    if grounding:
        context = "\n\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(grounding))
    else:
        context = "(no context retrieved; write from general knowledge and cite nothing)"
    return f"Learning objective:\n{objective}\n\nContext snippets:\n{context}"


def _resolve_citations(indices: list[int], grounding: list[RetrievalHit]) -> list[dict]:
    """Map cited snippet indices back to real chunk/source ids, dropping out-of-range ones."""
    seen: set[int] = set()
    citations: list[dict] = []
    for i in indices:
        if 0 <= i < len(grounding) and i not in seen:
            seen.add(i)
            hit = grounding[i]
            citations.append({"chunk_id": str(hit.chunk_id), "source_id": str(hit.source_id)})
    return citations


def _parse(content: str) -> _GeneratedBlock:
    """Extract and validate the JSON block, tolerating prose around the object."""
    try:
        data = json.loads(_extract_json(content))
    except json.JSONDecodeError as err:
        raise ContentGenerationError(f"unparseable block: {content!r}") from err
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ContentGenerationError(f"block has no body: {content!r}")
    citations = [c for c in data.get("citations", []) if isinstance(c, int)]
    return _GeneratedBlock(body=body, citations=citations)


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ContentGenerationError(f"no JSON object in block: {content!r}")
    return content[start : end + 1]


def _cache_key(
    learner_id: uuid.UUID,
    kc_ids: list[uuid.UUID],
    block_type: ContentType,
    grounding_ids: list[uuid.UUID],
) -> str:
    """Content-address a block over its learner, KCs, type, and grounding set."""
    payload = json.dumps(
        {
            "learner": str(learner_id),
            "kcs": sorted(str(k) for k in kc_ids),
            "type": str(block_type),
            "grounding": sorted(str(c) for c in grounding_ids),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
