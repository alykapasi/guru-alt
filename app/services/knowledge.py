"""CRUD over the knowledge graph.

Functions own their transactions (commit + refresh). Existence checks for clean 404s
live in the router; uniqueness/constraint violations surface as ``IntegrityError`` for
the router to translate into 409s.
"""

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.schemas.knowledge import KCCreate, SubjectCreate, TopicCreate

# --- Subjects ---------------------------------------------------------------


async def create_subject(session: AsyncSession, data: SubjectCreate) -> Subject:
    subject = Subject(slug=data.slug, name=data.name, description=data.description)
    session.add(subject)
    await session.commit()
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
