"""CRUD over the knowledge graph.

Functions own their transactions (commit + refresh). Existence checks for clean 404s
live in the router; uniqueness/constraint violations surface as ``IntegrityError`` for
the router to translate into 409s.
"""

import re
import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.source import Source
from app.schemas.knowledge import KCCreate, SubjectCreate, TopicCreate

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
) -> Subject:
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

    # Reassign owned sources to this subject
    if source_ids:
        for source_id in source_ids:
            source = await session.get(Source, source_id)
            if source is not None and source.learner_id == learner_id:
                source.subject_id = subject.id

    # Single atomic commit
    await session.commit()
    # Refresh to get all relationships populated
    await session.refresh(subject)
    return subject


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
