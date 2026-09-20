"""Requesting publication of a subject, and the snapshot that request freezes (S25b).

**The snapshot is the whole design.** A publication is judged by a human and materialized later,
and between those two moments the author can go on editing their subject. If approval read the
live subject, the reviewer would have approved one thing and shipped another — so the request
freezes exactly what would ship, as JSON, and approval reads only that (D1, D3).

**What travels is a short list, and everything else is excluded by construction.** The snapshot
carries topics, KCs, within-subject prerequisite edges, and the author's own items and rubrics on
those KCs. It carries no evidence, mastery, FSRS state or lesson plans; no notes, content blocks,
sources, chunks, conversations or memories; and no edge whose other end is a KC in a different
subject. The queries below are written to *select* that list rather than to select everything and
strip it, because a field added to a model later must not silently join the shipment.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Item, ItemKC, Rubric
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.publication import Publication, PublicationStatus

SOURCE_DERIVED_REFUSAL = "This subject was built from your uploaded material, which stays private."
NO_KCS_REFUSAL = "This subject has no components yet, so there is nothing to publish."
ALREADY_PENDING_REFUSAL = "This subject already has a publication request waiting for review."
NOT_PENDING_REFUSAL = "This request has already been decided."


class CannotPublish(ValueError):
    """The subject is not publishable, and the message says why in the author's terms."""


class AlreadyPending(ValueError):
    """One open request per subject — the partial unique index is the real enforcement."""


async def snapshot_of(session: AsyncSession, subject: Subject, author: Learner) -> dict:
    """Exactly what would ship if this were approved, as plain JSON-able data.

    Ids are kept as strings and keyed by their *source* id, which is what lets a reviewer
    exclude one item by id and what lets approval remap KC links onto the copies it creates.
    """
    topics = list(
        await session.scalars(
            select(Topic).where(Topic.subject_id == subject.id).order_by(Topic.slug)
        )
    )
    topic_ids = [topic.id for topic in topics]
    kcs = (
        list(await session.scalars(select(KC).where(KC.topic_id.in_(topic_ids)).order_by(KC.slug)))
        if topic_ids
        else []
    )
    kc_ids = {kc.id for kc in kcs}

    # Both ends inside this subject. An edge reaching a KC elsewhere would describe a graph the
    # reviewer is not looking at, and would name a component of the author's other work.
    edges = (
        list(
            await session.scalars(
                select(KCEdge).where(KCEdge.kc_id.in_(kc_ids), KCEdge.prereq_kc_id.in_(kc_ids))
            )
        )
        if kc_ids
        else []
    )

    items, rubrics = await _assessment_of(session, author, kc_ids)

    return {
        "subject": {"name": subject.name, "description": subject.description},
        "topics": [
            {
                "id": str(topic.id),
                "slug": topic.slug,
                "name": topic.name,
                "description": topic.description,
            }
            for topic in topics
        ],
        "kcs": [
            {
                "id": str(kc.id),
                "topic_id": str(kc.topic_id),
                "slug": kc.slug,
                "name": kc.name,
                "description": kc.description,
            }
            for kc in kcs
        ],
        "edges": [
            {
                "prereq_kc_id": str(edge.prereq_kc_id),
                "kc_id": str(edge.kc_id),
                "weight": edge.weight,
            }
            for edge in edges
        ],
        "items": items,
        "rubrics": rubrics,
    }


async def _assessment_of(
    session: AsyncSession, author: Learner, kc_ids: set[uuid.UUID]
) -> tuple[list[dict], list[dict]]:
    """The author's own items on these KCs, and the rubrics they need.

    Two restrictions, and both matter. *The author's own*: another learner's item on the same
    KC is their work, and shipping it would publish it without them ever being asked. *Only on
    these KCs*: an item the author owns which also tags a component of a different subject
    carries that subject's structure with it into the shared library.
    """
    if not kc_ids:
        return [], []

    candidates = list(
        await session.scalars(
            select(Item)
            .join(ItemKC, ItemKC.item_id == Item.id)
            .where(Item.owner_learner_id == author.id, ItemKC.kc_id.in_(kc_ids))
            .distinct()
        )
    )

    items: list[dict] = []
    rubric_ids: set[uuid.UUID] = set()
    for item in candidates:
        links = list(await session.scalars(select(ItemKC).where(ItemKC.item_id == item.id)))
        if any(link.kc_id not in kc_ids for link in links):
            continue  # reaches outside this subject — see the docstring
        if item.rubric_id is not None:
            rubric_ids.add(item.rubric_id)
        items.append(
            {
                "id": str(item.id),
                "item_type": item.item_type,
                "stem": item.stem,
                "answer_key": item.answer_key,
                "difficulty": item.difficulty,
                "rubric_id": str(item.rubric_id) if item.rubric_id else None,
                "origin": item.origin,
                "kc_weights": [{"kc_id": str(link.kc_id), "weight": link.weight} for link in links],
            }
        )

    rubric_rows = (
        list(
            await session.scalars(
                select(Rubric).where(
                    Rubric.id.in_(rubric_ids), Rubric.owner_learner_id == author.id
                )
            )
        )
        if rubric_ids
        else []
    )
    rubrics = [
        {
            "id": str(rubric.id),
            "kc_id": str(rubric.kc_id),
            "name": rubric.name,
            "criteria": rubric.criteria,
        }
        for rubric in rubric_rows
    ]
    return items, rubrics


async def request_publication(
    session: AsyncSession, subject: Subject, author: Learner, note: str | None
) -> Publication:
    """Freeze what would ship and put it in front of a reviewer.

    The caller has already established that ``author`` owns ``subject`` — this does not repeat
    the ownership gate, it enforces the rules that are about *publishability*.
    """
    if subject.private_source_derived:
        # No override anywhere, by design (D4). An override is a way around V03.
        raise CannotPublish(SOURCE_DERIVED_REFUSAL)

    pending = await session.scalar(
        select(Publication).where(
            Publication.source_subject_id == subject.id,
            Publication.status == PublicationStatus.PENDING,
        )
    )
    if pending is not None:
        # Checked here for a civil message; the partial unique index is what actually holds,
        # because two concurrent requests would both read "none pending" and both insert.
        raise AlreadyPending(ALREADY_PENDING_REFUSAL)

    snapshot = await snapshot_of(session, subject, author)
    if not snapshot["kcs"]:
        raise CannotPublish(NO_KCS_REFUSAL)

    publication = Publication(
        source_subject_id=subject.id,
        author_id=author.id,
        author_handle=author.handle,
        status=PublicationStatus.PENDING,
        author_note=note,
        snapshot=snapshot,
    )
    session.add(publication)
    await session.commit()
    await session.refresh(publication)
    return publication


async def cancel(session: AsyncSession, publication: Publication) -> Publication:
    """Withdraw a request before it is reviewed. The caller has established ownership."""
    if publication.status != PublicationStatus.PENDING:
        raise CannotPublish(NOT_PENDING_REFUSAL)
    publication.status = PublicationStatus.CANCELLED
    publication.reviewed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(publication)
    return publication


async def history_for(session: AsyncSession, subject: Subject) -> list[Publication]:
    """Every request made for this subject, newest first."""
    return list(
        await session.scalars(
            select(Publication)
            .where(Publication.source_subject_id == subject.id)
            .order_by(Publication.created_at.desc())
        )
    )


def is_curated_copy(subject: Subject) -> bool:
    """Whether this subject is a published copy rather than somebody's own or a seeded one."""
    return subject.owner_learner_id is None and subject.publication_id is not None


__all__ = [
    "ALREADY_PENDING_REFUSAL",
    "NOT_PENDING_REFUSAL",
    "NO_KCS_REFUSAL",
    "SOURCE_DERIVED_REFUSAL",
    "AlreadyPending",
    "CannotPublish",
    "cancel",
    "history_for",
    "is_curated_copy",
    "request_publication",
    "snapshot_of",
]
