"""Bring stale sources up to date (S50), deciding what is stale from the data every time.

Two things can make a source's chunks stale. A different embedding model makes their vectors
incomparable to new queries; the text is still right, so the chunks are re-embedded where they
are and every citation keeps resolving. A newer extraction/chunking pipeline may make the text
itself come out differently; only a re-ingest fixes that, which gives the chunks new ids, so it
happens only when the operator asks for it (``--reextract``). Scope repair brings legacy
subject/topic tags into line with the rule S55 enforces on new ones.

Nothing records progress: what is stale is read from the chunks, so a run that stops halfway is
resumed by running it again.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.llm import LLMClient, ModelRole
from app.models.knowledge import Subject, Topic
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag.pipeline import PIPELINE_VERSION, PartialEmbedding, embed_in_batches
from app.services import ingestion
from app.services.llm_log import log_llm_call

log = structlog.get_logger()


@dataclass(frozen=True)
class StaleSource:
    source_id: uuid.UUID
    learner_id: uuid.UUID
    origin: str
    chunks: int


@dataclass(frozen=True)
class ScopeRepair:
    source_id: uuid.UUID
    origin: str
    before: tuple[uuid.UUID | None, uuid.UUID | None]  # (subject_id, topic_id)
    after: tuple[uuid.UUID | None, uuid.UUID | None]


@dataclass
class ReindexPlan:
    reembed: list[StaleSource] = field(default_factory=list)
    reextract: list[StaleSource] = field(default_factory=list)
    scope: list[ScopeRepair] = field(default_factory=list)


@dataclass
class ReindexResult:
    reembedded: list[uuid.UUID] = field(default_factory=list)
    reextracted: list[uuid.UUID] = field(default_factory=list)
    busy: list[uuid.UUID] = field(default_factory=list)
    failed: dict[uuid.UUID, str] = field(default_factory=dict)
    scope_repaired: int = 0


async def _stale(
    session: AsyncSession, condition, learner_id: uuid.UUID | None, *, files_only: bool = False
) -> list[StaleSource]:
    stmt = (
        select(Source.id, Source.learner_id, Source.origin, func.count(Chunk.id))
        .join(Chunk, Chunk.source_id == Source.id)
        .where(Source.status == SourceStatus.DONE, Chunk.superseded_at.is_(None))
        .group_by(Source.id)
        .having(func.bool_or(condition))
        .order_by(Source.created_at)
    )
    if learner_id is not None:
        stmt = stmt.where(Source.learner_id == learner_id)
    if files_only:
        stmt = stmt.where(Source.kind == SourceKind.FILE)
    return [StaleSource(*row) for row in (await session.execute(stmt)).all()]


def repair(
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    subject_owner: uuid.UUID | None,
    subject_exists: bool,
    topic_id: uuid.UUID | None,
    topic_subject_id: uuid.UUID | None,
    topic_owner: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """The scope a source should have — S55's rule, applied to a row that predates it."""
    visible_subject = subject_exists and subject_owner in (None, learner_id)
    if subject_id is not None and not visible_subject:
        subject_id = None
    if topic_id is None:
        return subject_id, None
    if topic_owner not in (None, learner_id):
        return subject_id, None
    if subject_id is None:
        return topic_subject_id, topic_id
    return (subject_id, topic_id) if topic_subject_id == subject_id else (subject_id, None)


async def _scope_repairs(session: AsyncSession, learner_id: uuid.UUID | None) -> list[ScopeRepair]:
    own = aliased(Subject)
    topic_subject = aliased(Subject)
    stmt = (
        select(
            Source.id,
            Source.origin,
            Source.learner_id,
            Source.subject_id,
            own.id,
            own.owner_learner_id,
            Source.topic_id,
            Topic.subject_id,
            topic_subject.owner_learner_id,
        )
        .outerjoin(own, own.id == Source.subject_id)
        .outerjoin(Topic, Topic.id == Source.topic_id)
        .outerjoin(topic_subject, topic_subject.id == Topic.subject_id)
        .where((Source.subject_id.is_not(None)) | (Source.topic_id.is_not(None)))
    )
    if learner_id is not None:
        stmt = stmt.where(Source.learner_id == learner_id)
    repairs = []
    for sid, origin, owner, subj, subj_row, subj_owner, topic, t_subj, t_owner in (
        await session.execute(stmt)
    ).all():
        after = repair(
            learner_id=owner,
            subject_id=subj,
            subject_owner=subj_owner,
            subject_exists=subj_row is not None,
            topic_id=topic,
            topic_subject_id=t_subj,
            topic_owner=t_owner,
        )
        if after != (subj, topic):
            repairs.append(ScopeRepair(sid, origin, (subj, topic), after))
    return repairs


async def plan(
    session: AsyncSession, *, space: str, learner_id: uuid.UUID | None = None
) -> ReindexPlan:
    """What is stale right now. Changes nothing."""
    return ReindexPlan(
        reembed=await _stale(session, Chunk.embedding_space != space, learner_id),
        # Files only: v0 refuses URL ingestion, so a legacy URL source can never be re-extracted,
        # and listing it would fail every --reextract run at the same source forever.
        reextract=await _stale(
            session, Chunk.pipeline_version < PIPELINE_VERSION, learner_id, files_only=True
        ),
        scope=await _scope_repairs(session, learner_id),
    )


async def _reembed(
    session: AsyncSession, llm: LLMClient, stale: StaleSource, *, space: str, settings: Settings
) -> None:
    chunks = list(
        (
            await session.scalars(
                select(Chunk)
                .where(Chunk.source_id == stale.source_id, Chunk.superseded_at.is_(None))
                .order_by(Chunk.ordinal)
            )
        ).all()
    )
    try:
        embedded = await embed_in_batches(
            llm,
            [c.text for c in chunks],
            batch_size=settings.embed_batch_size,
            concurrency=settings.embed_concurrency,
        )
    except PartialEmbedding as exc:
        if exc.usage.total_tokens:
            await log_llm_call(
                learner_id=stale.learner_id,
                role=str(ModelRole.EMBED),
                spec=llm.spec(ModelRole.EMBED),
                usage=exc.usage,
            )
        raise
    for chunk, vector in zip(chunks, embedded.vectors, strict=True):
        chunk.embedding = vector
        chunk.embedding_space = space
    if embedded.usage.total_tokens:
        await log_llm_call(
            learner_id=stale.learner_id,
            role=str(ModelRole.EMBED),
            spec=llm.spec(ModelRole.EMBED),
            usage=embedded.usage,
        )
    await session.commit()


async def apply(
    session: AsyncSession,
    llm: LLMClient,
    found: ReindexPlan,
    *,
    space: str,
    reextract: bool,
    limit: int | None,
    enqueue: Callable[[uuid.UUID], Awaitable[None]],
    settings: Settings,
) -> ReindexResult:
    """Carry out ``found``. One source's failure is recorded and the run moves on."""
    result = ReindexResult()
    for fix in found.scope:
        source = await session.get(Source, fix.source_id)
        if source is not None:
            source.subject_id, source.topic_id = fix.after
            result.scope_repaired += 1
    await session.commit()

    budget = limit if limit is not None else len(found.reembed) + len(found.reextract)
    reextracting = {s.source_id for s in found.reextract} if reextract else set()
    if reextract:
        for stale in found.reextract:
            if budget <= 0:
                break
            budget -= 1
            reset = await ingestion.reset_for_reingest(session, stale.source_id)
            if reset is None:
                result.busy.append(stale.source_id)
                continue
            await ingestion.dispatch(enqueue, stale.source_id)
            result.reextracted.append(stale.source_id)
    for stale in found.reembed:
        if stale.source_id in reextracting:
            continue
        if budget <= 0:
            break
        budget -= 1
        try:
            await _reembed(session, llm, stale, space=space, settings=settings)
        except Exception as exc:  # reported, and the next run retries it
            await session.rollback()
            log.warning("reindex.reembed_failed", source_id=str(stale.source_id), error=str(exc))
            result.failed[stale.source_id] = str(exc)
            continue
        result.reembedded.append(stale.source_id)
    return result


def render(found: ReindexPlan, result: ReindexResult | None) -> str:
    """The operator's report: what is stale, and what this run did about it."""
    lines = [
        f"re-embed: {len(found.reembed)} sources, "
        f"{sum(s.chunks for s in found.reembed)} chunks (embedding model changed)",
        *(f"  {s.source_id}  {s.origin}  {s.chunks} chunks" for s in found.reembed),
        f"re-extract: {len(found.reextract)} sources (pipeline version below {PIPELINE_VERSION})",
        *(f"  {s.source_id}  {s.origin}  {s.chunks} chunks" for s in found.reextract),
        f"scope repair: {len(found.scope)} sources",
        *(f"  {r.source_id}  {r.origin}  {r.before} -> {r.after}" for r in found.scope),
    ]
    if result is None:
        lines.append("dry run: nothing changed. --apply to re-embed and repair scope.")
    else:
        lines += [
            f"re-embedded {len(result.reembedded)}, re-extract queued {len(result.reextracted)}, "
            f"scope repaired {result.scope_repaired}",
            *(f"  busy (skipped): {sid}" for sid in result.busy),
            *(f"  failed: {sid}  {err}" for sid, err in result.failed.items()),
        ]
    return "\n".join(lines)
