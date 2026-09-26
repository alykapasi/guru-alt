"""Content engine: grounded, KC-tagged, cached block generation + assembly (§6.3).

``generate_block`` retrieves grounding chunks for a KC, asks the role-appropriate model to
write the block **citing only those chunks**, and caches the result under a content-addressed
``cache_key`` so an identical request reuses it. ``assemble`` returns the standard set of
blocks for a KC, generating any that are missing. All model access is by role; the service
owns the transaction and logs each call's cost.

The cache key hashes *the exact prompts the model will be sent and the exact model that will
answer them* (S29), rather than a summary of the inputs those prompts were built from. The
distinction matters because a key that omits any determinant of the output does not merely
miss a refresh — it serves the pre-change block forever, and does so silently: the block still
renders and still cites real chunks, but it was written to instructions, a model, or an
objective that no longer exists. Deriving the key from the rendered prompts means editing a
prompt, retitling a KC, or repointing a role at a different model each invalidates on its own,
with nothing to remember to bump.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import citation_support
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.llm.registry import ModelSpec
from app.models.content import ContentBlock, ContentType
from app.models.knowledge import KC, Topic
from app.models.source import Chunk
from app.rag import retrieval
from app.rag.extraction_quality import reading_note
from app.rag.retrieval import RetrievalHit
from app.rag.scope import resolve_scope
from app.services import grounding as grounding_policy
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
    "You are an expert instructional author. Write {guidance} for the learning objective from "
    "the numbered context snippets. Ground every claim you can in the context and cite the "
    "snippets you used by their index. {scope_rule} If the snippets disagree with one another, "
    "say so in the body and cite both rather than silently choosing one. Respond with ONLY a "
    "JSON object of the form "
    '{{"body": "<the content>", "citations": [<indices of snippets used>]}} and nothing else.'
)

# The two scope rules (S26). Normal mode may fill a gap from general knowledge but has to say
# which part that is; sources-only names the gap instead of filling it.
_SUPPLEMENT_RULE = (
    "Where the snippets leave a gap the objective needs, you may fill it from general "
    "knowledge, but say plainly in the body which part does not come from the learner's "
    "materials."
)
_SOURCES_ONLY_RULE = (
    "Use ONLY the numbered context snippets: where they leave a gap, say in the body what they "
    "do not cover rather than filling it from general knowledge."
)

# The instruction for a request with nothing retrieved (S28). Previously one system prompt
# served both cases — "using ONLY the numbered context snippets" — while the user message for
# an empty retrieval said "write from general knowledge and cite nothing". The model was told
# to do two incompatible things in the same request, and which one it obeyed was not decided
# anywhere. This states the situation once, in the place that describes the task.
_UNGROUNDED_SYSTEM_PROMPT = (
    "You are an expert instructional author. No source material was retrieved for this "
    "learning objective, so write {guidance} from established general knowledge. Say plainly "
    "within the body that it does not draw on the learner's own materials. Cite nothing: "
    "there are no snippets, so any index would point at material that does not exist. Respond "
    'with ONLY a JSON object of the form {{"body": "<the content>", "citations": []}} and '
    "nothing else."
)


class ContentGenerationError(RuntimeError):
    """The model's reply could not be parsed into a content block."""


class NoSourceCoverage(LookupError):
    """Sources-only, and nothing in the subject's sources matched this KC (S26).

    Raised before any model call: there is nothing the block would be allowed to say.
    """

    def __init__(self, kc_id: uuid.UUID) -> None:
        super().__init__(f"no source coverage for KC {kc_id}")
        self.kc_id = kc_id


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

    Retrieves grounding chunks, renders the prompts, and returns the existing block if one is
    cached for that exact (learner, KC, type, grounding, model, prompt) — otherwise generates,
    persists, logs cost, and commits.

    The prompts are built once here and handed to both the key and the model, so the two cannot
    describe different requests: a key computed from separately re-rendered text would drift
    from what was actually sent the moment either construction changed.
    """
    kc = await session.get(KC, kc_id)
    if kc is None:
        raise LookupError(f"KC {kc_id} not found")

    # Scoped by the KC's subject through the one rule every path uses (S26): its own sources,
    # plus untagged ones only if the subject opted in. Before this, lessons alone admitted every
    # untagged upload, which V05 rules out: unassigned material is not added silently.
    subject_id = await session.scalar(select(Topic.subject_id).where(Topic.id == kc.topic_id))
    scope = await resolve_scope(session, learner_id=learner_id, subject_id=subject_id)
    assert scope is not None, "a KC always belongs to a subject"
    grounding = await retrieval.retrieve(
        session, llm, _kc_query(kc), scope=scope, limit=grounding_k
    )
    if not grounding and scope.sources_only:
        raise NoSourceCoverage(kc_id)
    role = _ROLE_BY_TYPE[block_type]
    if grounding:
        rule = _SOURCES_ONLY_RULE if scope.sources_only else _SUPPLEMENT_RULE
        system = _SYSTEM_PROMPT.format(guidance=_GUIDANCE[block_type], scope_rule=rule)
        if any(reading_note(hit.provenance) for hit in grounding):
            system = f"{system} {grounding_policy.READING_NOTE_RULE}"
    else:
        system = _UNGROUNDED_SYSTEM_PROMPT.format(guidance=_GUIDANCE[block_type])
    user = _build_prompt(kc, grounding)
    cache_key = _cache_key(
        learner_id,
        [kc_id],
        block_type,
        [h.chunk_id for h in grounding],
        spec=llm.spec(role),
        system=system,
        user=user,
    )

    existing = await session.scalar(select(ContentBlock).where(ContentBlock.cache_key == cache_key))
    if existing is not None:
        return existing

    parsed, usage = await _generate(llm, role, system=system, user=user)
    block = ContentBlock(
        learner_id=learner_id,
        kc_ids=[kc_id],
        block_type=block_type,
        body=parsed.body,
        citations=_resolve_citations(parsed.citations, grounding),
        cache_key=cache_key,
        model=llm.spec(role).model,
        grounding_count=len(grounding),
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


async def _cited_passages(
    session: AsyncSession, block: ContentBlock
) -> list[citation_support.CitedPassage]:
    """The passages a block cites, in citation order, read by id — superseded ones included,
    since a re-ingest keeps cited chunks as history precisely so this still works (S29). A
    chunk that no longer exists at all drops out."""
    ids = [uuid.UUID(c["chunk_id"]) for c in block.citations if c.get("chunk_id")]
    rows = (
        list((await session.scalars(select(Chunk).where(Chunk.id.in_(ids)))).all()) if ids else []
    )
    by_id = {chunk.id: chunk for chunk in rows}
    return [
        citation_support.CitedPassage(chunk_id=chunk.id, source_id=chunk.source_id, text=chunk.text)
        for chunk in (by_id.get(i) for i in ids)
        if chunk is not None
    ]


async def check_block_citations(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    block_id: uuid.UUID,
) -> citation_support.SupportReport:
    """Check whether a stored block's claims are carried by the passages it cited (S28).

    Explicitly invoked and paid for, never part of generation: it is a model call on the SMART
    tier per block, and nothing in the system has decided what rate of unsupported claims is
    tolerable. Measuring first, gating later — if at all.

    Cited chunks are re-read from the database rather than trusted from the block, and a
    citation whose chunk has since been deleted simply drops out. Re-ingestion replaces chunks,
    so a block outliving its grounding is ordinary rather than exceptional, and checking it
    against passages that no longer exist would be checking it against nothing.
    """
    block = await session.get(ContentBlock, block_id)
    if block is None or block.learner_id != learner_id:
        raise LookupError(f"content block {block_id} not found")

    passages = await _cited_passages(session, block)
    report, usage = await citation_support.check_support(llm, body=block.body, passages=passages)
    if usage.total_tokens:
        await log_llm_call(
            learner_id=learner_id,
            role=str(citation_support.SUPPORT_ROLE),
            spec=llm.spec(citation_support.SUPPORT_ROLE),
            usage=usage,
        )
    return report


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
    llm: LLMClient, role: ModelRole, *, system: str, user: str
) -> tuple[_GeneratedBlock, Usage]:
    """Call the model and parse its reply into a body + cited snippet indices.

    Takes the rendered prompts rather than the material they are rendered from: they are the
    same strings the cache key was computed over, which is what keeps the two in step.
    """
    completion = await llm.complete(
        role,
        [ChatMessage(role=ChatRole.USER, content=user)],
        system=system,
        max_tokens=1024,
    )
    return _parse(completion.content), completion.usage


def _build_prompt(kc: KC, grounding: list[RetrievalHit]) -> str:
    """The user message: the objective, and the snippets if there are any.

    With nothing retrieved there is no context section at all, rather than a parenthetical
    telling the model what to do instead. Instructions belong in the system prompt — a user
    message that also gave them is how the two came to disagree (S28).
    """
    objective = _kc_query(kc)
    if not grounding:
        return f"Learning objective:\n{objective}"
    context = "\n\n".join(grounding_policy.passage(i, hit) for i, hit in enumerate(grounding))
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
    *,
    spec: ModelSpec,
    system: str,
    user: str,
) -> str:
    """Content-address a block over everything that decides what the model writes.

    ``system`` and ``user`` are the rendered prompts, so the KC's own wording, the chunks'
    text, the instructions for this block type and the shape of the prompt template are all
    inside the key by construction — none of them needs its own field here, and none can be
    changed without changing the key.

    ``spec`` is separate because it is the one determinant that is not in the prompts: the
    same request answered by a different model is a different block, and a role repointed from
    one model to another must not keep serving the old one's writing.

    ``grounding_ids`` is kept even though the chunks' text is already inside ``user``. Two
    distinct chunks can hold identical text, and ``citations`` resolves to chunk and source
    ids — so without the ids, a cached block could cite a source the request never retrieved.
    """
    payload = json.dumps(
        {
            "learner": str(learner_id),
            "kcs": sorted(str(k) for k in kc_ids),
            "type": str(block_type),
            "grounding": sorted(str(c) for c in grounding_ids),
            "model": f"{spec.provider}:{spec.model}",
            "system": system,
            "user": user,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
