"""CRUD over the knowledge graph.

Functions own their transactions (commit + refresh). Existence checks for clean 404s
live in the router; uniqueness/constraint violations surface as ``IntegrityError`` for
the router to translate into 409s.
"""

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.source import Chunk, ChunkKC, Source
from app.schemas.knowledge import KCCreate, SubjectCreate, TopicCreate


@dataclass(frozen=True)
class CurriculumResult:
    """The new subject, plus the sources whose scope this call invalidated.

    The caller needs the second list: moving a source between subjects makes its chunk KC tags
    describe the wrong graph, and rebuilding them is a model call per chunk — background work,
    not something to hold a curriculum-creation request open for.
    """

    subject: Subject
    reassigned_source_ids: list[uuid.UUID]


# --- Helpers ----------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a slug: lowercase, remove non-word chars, collapse spaces/hyphens to -."""
    text = text.strip().lower()
    # Replace non-word chars (except hyphens) with nothing
    text = re.sub(r"[^\w\s-]", "", text)
    # Collapse multiple spaces/hyphens to single hyphen
    text = re.sub(r"[-\s]+", "-", text)
    # Remove trailing hyphen
    text = text.rstrip("-")
    return text


async def subject_name_exists(session: AsyncSession, name: str) -> bool:
    """Check if a subject with this name exists (case-insensitive)."""
    result = await session.scalar(
        select(Subject).where(func.lower(Subject.name) == name.strip().lower()).limit(1)
    )
    return result is not None


# --- Subjects ---------------------------------------------------------------


async def create_subject(session: AsyncSession, data: SubjectCreate) -> Subject:
    subject = Subject(slug=data.slug, name=data.name, description=data.description)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    return subject


async def create_subject_with_graph(
    session: AsyncSession,
    subject_name: str,
    subject_description: str | None,
    topics_data: list[dict],
    source_ids: list[uuid.UUID] | None,
    learner_id: uuid.UUID,
) -> CurriculumResult:
    """Create a Subject with Topics and KCs in one atomic transaction.

    Handles multi-level slug deduplication and reassigns owned sources.
    - Subject slug: globally unique (dedups across all subjects)
    - Topic slug: unique within the subject (dedups within topics_data)
    - KC slug: unique within its topic (dedups within topic's kcs)

    Args:
        session: Database session
        subject_name: Name of the subject (will be slugified)
        subject_description: Optional subject description
        topics_data: List of dicts with "name", "description", "kcs" keys.
                     Each kc dict has "name" and "description".
        source_ids: Optional list of source IDs to reassign to this subject.
                    Only reassigns if owned by learner_id.
        learner_id: ID of the learner for source ownership check.

    Returns:
        The created Subject (with id set, relationships populated).
    """
    # Deduplicate subject slug at the global level
    base_slug = _slugify(subject_name)
    subject_slug = base_slug
    result = await session.execute(select(Subject.slug))
    existing_subject_slugs = {slug for (slug,) in result.all()}
    counter = 2
    while subject_slug in existing_subject_slugs:
        subject_slug = f"{base_slug}_{counter}"
        counter += 1

    # Create the subject and flush to get its ID
    subject = Subject(
        slug=subject_slug,
        name=subject_name,
        description=subject_description,
    )
    session.add(subject)
    await session.flush()

    # Deduplicate and create topics
    topic_seen_slugs: set[str] = set()
    for topic_data in topics_data:
        topic_name = topic_data.get("name", "")
        topic_desc = topic_data.get("description")
        kcs_data = topic_data.get("kcs", [])

        # Deduplicate topic slug within this subject
        base_topic_slug = _slugify(topic_name)
        topic_slug = base_topic_slug
        counter = 2
        while topic_slug in topic_seen_slugs:
            topic_slug = f"{base_topic_slug}_{counter}"
            counter += 1
        topic_seen_slugs.add(topic_slug)

        topic = Topic(
            subject_id=subject.id,
            slug=topic_slug,
            name=topic_name,
            description=topic_desc,
        )
        session.add(topic)
        await session.flush()

        # Deduplicate and create KCs within this topic
        kc_seen_slugs: set[str] = set()
        for kc_data in kcs_data:
            kc_name = kc_data.get("name", "")
            kc_desc = kc_data.get("description")

            # Deduplicate KC slug within this topic
            base_kc_slug = _slugify(kc_name)
            kc_slug = base_kc_slug
            counter = 2
            while kc_slug in kc_seen_slugs:
                kc_slug = f"{base_kc_slug}_{counter}"
                counter += 1
            kc_seen_slugs.add(kc_slug)

            kc = KC(
                topic_id=topic.id,
                slug=kc_slug,
                name=kc_name,
                description=kc_desc,
            )
            session.add(kc)
        await session.flush()

    # Reassign owned sources to this subject.
    reassigned: list[uuid.UUID] = []
    if source_ids:
        for source_id in source_ids:
            source = await session.get(Source, source_id)
            if source is None or source.learner_id != learner_id:
                continue
            if source.subject_id == subject.id:
                continue  # nothing moved, so nothing derived from it is stale
            source.subject_id = subject.id
            # A topic from the old subject cannot describe a source in this one, and a source
            # scoped to a topic outside its subject is retrievable through neither filter.
            source.topic_id = None
            reassigned.append(source.id)
    # Chunk KC tags name KCs in the *previous* graph, so after a move they are worse than
    # absent: they assert this material teaches concepts it was never read against. Drop them
    # here, in the same transaction as the move, and let retagging rebuild them.
    if reassigned:
        await session.execute(
            delete(ChunkKC).where(
                ChunkKC.chunk_id.in_(select(Chunk.id).where(Chunk.source_id.in_(reassigned)))
            )
        )

    # Single atomic commit
    await session.commit()
    # Refresh to get all relationships populated
    await session.refresh(subject)
    return CurriculumResult(subject=subject, reassigned_source_ids=reassigned)


class ScopeConflict(ValueError):
    """A source was scoped to a topic that does not belong to its subject."""


async def resolve_source_scope(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None,
    topic_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Check a source's scope is internally consistent, filling in what it implies.

    A topic belongs to exactly one subject, so a source claiming both must agree with the
    graph. Nothing checked this: a source could sit in a topic from a different subject, and
    since retrieval filters on *both*, such a source was reachable through neither — indexed,
    embedded, paid for, and invisible.

    A topic given without a subject is not an error; the topic determines the subject, so it
    is filled in rather than rejected.
    """
    if topic_id is None:
        return subject_id, None
    topic = await session.get(Topic, topic_id)
    if topic is None:
        raise ScopeConflict(f"topic {topic_id} does not exist")
    if subject_id is not None and topic.subject_id != subject_id:
        raise ScopeConflict(
            f"topic {topic_id} belongs to subject {topic.subject_id}, not {subject_id}"
        )
    return topic.subject_id, topic_id


async def list_subjects(session: AsyncSession) -> Sequence[Subject]:
    result = await session.scalars(select(Subject).order_by(Subject.slug))
    return result.all()


async def get_subject(session: AsyncSession, subject_id: uuid.UUID) -> Subject | None:
    return await session.get(Subject, subject_id)


# --- Topics -----------------------------------------------------------------


async def create_topic(session: AsyncSession, subject_id: uuid.UUID, data: TopicCreate) -> Topic:
    topic = Topic(
        subject_id=subject_id,
        slug=data.slug,
        name=data.name,
        description=data.description,
    )
    session.add(topic)
    await session.commit()
    await session.refresh(topic)
    return topic


async def list_topics(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[Topic]:
    result = await session.scalars(
        select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.slug)
    )
    return result.all()


async def get_topic(session: AsyncSession, topic_id: uuid.UUID) -> Topic | None:
    return await session.get(Topic, topic_id)


# --- KCs --------------------------------------------------------------------


async def create_kc(session: AsyncSession, topic_id: uuid.UUID, data: KCCreate) -> KC:
    kc = KC(
        topic_id=topic_id,
        slug=data.slug,
        name=data.name,
        description=data.description,
    )
    session.add(kc)
    await session.commit()
    await session.refresh(kc)
    return kc


async def list_kcs(session: AsyncSession, topic_id: uuid.UUID) -> Sequence[KC]:
    result = await session.scalars(select(KC).where(KC.topic_id == topic_id).order_by(KC.slug))
    return result.all()


async def get_kc(session: AsyncSession, kc_id: uuid.UUID) -> KC | None:
    return await session.get(KC, kc_id)


async def list_kcs_for_subject(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[KC]:
    """Every KC across a subject's topics (placement's candidate pool)."""
    result = await session.scalars(
        select(KC)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id)
        .order_by(Topic.slug, KC.slug)
    )
    return result.all()


async def list_root_kcs(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[KC]:
    """KCs in a subject with no prerequisite from another KC in the same subject.

    The highest-leverage, most-foundational sample for a placement light test.
    """
    subject_kc_ids = (
        select(KC.id).join(Topic, KC.topic_id == Topic.id).where(Topic.subject_id == subject_id)
    )
    dependent_ids = select(KCEdge.kc_id).where(KCEdge.prereq_kc_id.in_(subject_kc_ids))
    result = await session.scalars(
        select(KC)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id, KC.id.notin_(dependent_ids))
        .order_by(Topic.slug, KC.slug)
    )
    return result.all()


# --- Prerequisite edges -----------------------------------------------------


async def add_prerequisite(
    session: AsyncSession, kc_id: uuid.UUID, prereq_kc_id: uuid.UUID, weight: float
) -> KCEdge:
    edge = KCEdge(kc_id=kc_id, prereq_kc_id=prereq_kc_id, weight=weight)
    session.add(edge)
    await session.commit()
    await session.refresh(edge)
    return edge


async def list_prerequisites(session: AsyncSession, kc_id: uuid.UUID) -> Sequence[KCEdge]:
    result = await session.scalars(select(KCEdge).where(KCEdge.kc_id == kc_id))
    return result.all()


async def list_edges_for_subject(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[KCEdge]:
    """Every prerequisite edge within a subject — the lesson plan's in-memory closure/topo-sort
    needs the whole edge set at once rather than one KC at a time."""
    result = await session.scalars(
        select(KCEdge)
        .join(KC, KCEdge.kc_id == KC.id)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id)
    )
    return result.all()


async def subjects_for_kcs(session: AsyncSession, kc_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Distinct subject ids that own any of ``kc_ids`` (KC -> Topic -> Subject)."""
    kc_ids = list(kc_ids)
    if not kc_ids:
        return set()
    result = await session.scalars(
        select(Topic.subject_id)
        .join(KC, KC.topic_id == Topic.id)
        .where(KC.id.in_(kc_ids))
        .distinct()
    )
    return set(result.all())


@dataclass(frozen=True)
class KCCoverage:
    """How much of a learner's own library is tagged to one KC."""

    kc_id: uuid.UUID
    slug: str
    name: str
    chunk_count: int


async def kc_coverage(
    session: AsyncSession, *, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> list[KCCoverage]:
    """Per-KC count of the learner's chunks tagged to it, zeros included.

    Ingestion pays a FAST model call per chunk to write ``ChunkKC`` rows, and until this
    nothing read them — the tags were a recurring bill with no consumer (S55). This is the
    cheapest honest consumer: it answers "can this KC be taught from what the learner actually
    uploaded, or only from the model's own knowledge?", which is a question the planner and
    the learner both have, and it does it without letting an unvalidated signal touch
    retrieval ranking. Whether tags *improve retrieval* is a separate question that needs a
    real corpus to answer, and is deliberately not assumed here.

    Zero-coverage KCs are kept: a gap in the library is the more actionable half of the answer.
    """
    # A correlated subquery rather than a chain of outer joins: with joins, a KC covered only
    # by *another* learner's chunks produced a row that the ownership filter then removed,
    # taking the KC out of the report entirely instead of showing it as uncovered. Counting
    # per KC keeps every KC unconditionally, which is the whole point.
    covered = (
        select(func.count(func.distinct(Chunk.id)))
        .select_from(ChunkKC)
        .join(Chunk, Chunk.id == ChunkKC.chunk_id)
        .join(Source, Source.id == Chunk.source_id)
        .where(ChunkKC.kc_id == KC.id, Source.learner_id == learner_id)
        .correlate(KC)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(KC.id, KC.slug, KC.name, covered)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id)
        .order_by(KC.slug)
    )
    return [
        KCCoverage(kc_id=kc_id, slug=slug, name=name, chunk_count=count)
        for kc_id, slug, name, count in rows.all()
    ]
