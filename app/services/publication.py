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

from app.models.assessment import AssessmentVisibility, Item, ItemKC, ItemOrigin, Rubric
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.publication import Publication, PublicationStatus
from app.services import knowledge

SOURCE_DERIVED_REFUSAL = "This subject was built from your uploaded material, which stays private."
NO_KCS_REFUSAL = "This subject has no components yet, so there is nothing to publish."
ALREADY_PENDING_REFUSAL = "This subject already has a publication request waiting for review."
NOT_PENDING_REFUSAL = "This request has already been decided."
NOT_PUBLISHED_REFUSAL = "Only a published subject can be withdrawn."
WITHDRAW_REASON_REFUSAL = "Withdrawing needs a reason, so the decision can be explained later."


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


REJECTION_NOTE_MIN = 8
SHORT_NOTE_REFUSAL = (
    f"A rejection needs a reason the author can act on — at least {REJECTION_NOTE_MIN} characters."
)
NOT_PENDING_REVIEW_REFUSAL = "This request has already been decided."


async def approve(
    session: AsyncSession,
    publication: Publication,
    reviewer: Learner,
    *,
    excluded_item_ids: list[uuid.UUID] | None = None,
    note: str | None = None,
) -> Subject:
    """Materialize the snapshot as a curated subject, in one transaction (D3).

    **It reads the snapshot, never the live subject.** That is the single most important line
    in this function: the author may have edited, deleted or rebuilt their subject since asking,
    and approval that re-read it would ship something no reviewer ever saw. Nothing below
    touches ``publication.source_subject_id``'s current state.

    Topics and KCs are built directly rather than through ``create_topic``/``create_kc``,
    because those commit per row and this has to be one transaction — but each KC still goes
    through ``resolve_concept``, which is the part of the ordinary path that carries a rule
    (S24 owns concept identity, and a copy that skipped it would be invisible to every query
    that reasons about what a learner already knows).
    """
    if publication.status != PublicationStatus.PENDING:
        raise CannotPublish(NOT_PENDING_REVIEW_REFUSAL)

    excluded = {str(item_id) for item_id in (excluded_item_ids or [])}
    snapshot = publication.snapshot

    copy = Subject(
        slug=await knowledge.unique_subject_slug(
            session, snapshot["subject"]["name"], owner_learner_id=None
        ),
        name=snapshot["subject"]["name"],
        description=snapshot["subject"].get("description"),
        # NULL owner is what *makes* it curated (S25). It carries the decision that created it.
        owner_learner_id=None,
        publication_id=publication.id,
    )
    session.add(copy)
    await session.flush()

    topics: dict[str, Topic] = {}
    for entry in snapshot.get("topics", []):
        topic = Topic(
            subject_id=copy.id,
            slug=entry["slug"],
            name=entry["name"],
            description=entry.get("description"),
        )
        session.add(topic)
        topics[entry["id"]] = topic
    await session.flush()

    kcs: dict[str, KC] = {}
    for entry in snapshot.get("kcs", []):
        parent = topics.get(entry["topic_id"])
        if parent is None:
            continue  # a KC whose topic is not in the snapshot has nowhere to live
        concept = await knowledge.resolve_concept(session, entry["name"])
        kc = KC(
            topic_id=parent.id,
            slug=entry["slug"],
            name=entry["name"],
            description=entry.get("description"),
            concept_id=concept.id if concept else None,
        )
        session.add(kc)
        kcs[entry["id"]] = kc
    await session.flush()

    for entry in snapshot.get("edges", []):
        prereq, dependent = kcs.get(entry["prereq_kc_id"]), kcs.get(entry["kc_id"])
        if prereq is None or dependent is None:
            continue
        session.add(
            KCEdge(prereq_kc_id=prereq.id, kc_id=dependent.id, weight=entry.get("weight", 1.0))
        )

    rubrics: dict[str, Rubric] = {}
    for entry in snapshot.get("rubrics", []):
        kc = kcs.get(entry["kc_id"])
        if kc is None:
            continue
        rubric = Rubric(
            visibility=AssessmentVisibility.CURATED,
            owner_learner_id=None,
            kc_id=kc.id,
            name=entry.get("name"),
            criteria=entry.get("criteria") or {},
        )
        session.add(rubric)
        rubrics[entry["id"]] = rubric
    await session.flush()

    for entry in snapshot.get("items", []):
        if entry["id"] in excluded:
            continue
        links = [
            (kcs[link["kc_id"]], link.get("weight", 1.0))
            for link in entry.get("kc_weights", [])
            if link["kc_id"] in kcs
        ]
        if not links:
            continue  # an item with nothing to attach to would be unreachable anyway
        rubric = rubrics.get(entry["rubric_id"]) if entry.get("rubric_id") else None
        session.add(
            Item(
                visibility=AssessmentVisibility.CURATED,
                owner_learner_id=None,
                item_type=entry["item_type"],
                stem=entry["stem"],
                answer_key=entry.get("answer_key"),
                difficulty=entry.get("difficulty", 0.0),
                rubric_id=rubric.id if rubric else None,
                # Provenance is preserved while ownership is not: who wrote it stays true, and
                # it never grants authority over the shared copy (see `ItemOrigin`).
                origin=entry.get("origin", ItemOrigin.LEARNER),
                author_learner_id=publication.author_id,
                kc_links=[ItemKC(kc_id=kc.id, weight=weight) for kc, weight in links],
            )
        )

    # A newer version replaces the author's previous one (D7). Unlisting, not removal: the
    # older copy stays reachable by id and stays in the catalog of anybody already studying it,
    # because it was reviewed as shareable and their plan points at it.
    await _supersede_previous(session, publication, copy)

    publication.status = PublicationStatus.APPROVED
    publication.reviewer_id = reviewer.id
    publication.reviewer_handle = reviewer.handle
    publication.reviewed_at = datetime.now(UTC)
    publication.review_note = note
    publication.excluded_item_ids = sorted(excluded)
    publication.published_subject_id = copy.id

    await session.commit()
    await session.refresh(copy)
    return copy


async def reject(
    session: AsyncSession, publication: Publication, reviewer: Learner, note: str
) -> Publication:
    """Refuse a request, with a reason the author can act on.

    The note is required and has a floor, because "no" with nothing attached tells an author
    only that somebody looked — they cannot fix what they are not told about, and the next
    request will be the same one.
    """
    if publication.status != PublicationStatus.PENDING:
        raise CannotPublish(NOT_PENDING_REVIEW_REFUSAL)
    if len(note.strip()) < REJECTION_NOTE_MIN:
        raise CannotPublish(SHORT_NOTE_REFUSAL)

    publication.status = PublicationStatus.REJECTED
    publication.reviewer_id = reviewer.id
    publication.reviewer_handle = reviewer.handle
    publication.reviewed_at = datetime.now(UTC)
    publication.review_note = note
    await session.commit()
    await session.refresh(publication)
    return publication


async def pending(session: AsyncSession, status: str | None = None) -> list[Publication]:
    """The review queue, oldest first — the order somebody works through it in."""
    statement = select(Publication).order_by(Publication.created_at)
    if status is not None:
        statement = statement.where(Publication.status == status)
    return list(await session.scalars(statement))


async def _supersede_previous(
    session: AsyncSession, publication: Publication, copy: Subject
) -> None:
    """Point this author's previously published copy at the new one.

    Scoped to the *same source subject*, not merely the same author: publishing a second,
    unrelated subject is not a new version of the first, and treating it as one would unlist
    somebody's Calculus because they later shared their Physics.
    """
    if publication.source_subject_id is None:
        return
    previous = list(
        await session.scalars(
            select(Subject)
            .join(Publication, Publication.id == Subject.publication_id)
            .where(
                Publication.source_subject_id == publication.source_subject_id,
                Publication.id != publication.id,
                Subject.id != copy.id,
                Subject.superseded_by_id.is_(None),
            )
        )
    )
    for older in previous:
        older.superseded_by_id = copy.id


async def withdraw(session: AsyncSession, subject: Subject, reason: str) -> Subject:
    """Unlist a published subject, recording why.

    Only a published copy: withdrawing a learner's own subject would be a deletion wearing a
    different name, and withdrawing a seeded curated subject is an operator's job for a
    migration, not a review action. The reason is required because a subject that vanished
    from the catalog with no recorded cause is one nobody can answer questions about later.
    """
    if not is_curated_copy(subject):
        raise CannotPublish(NOT_PUBLISHED_REFUSAL)
    if not reason.strip():
        raise CannotPublish(WITHDRAW_REASON_REFUSAL)
    subject.withdrawn_at = datetime.now(UTC)
    subject.withdrawn_reason = reason
    await session.commit()
    await session.refresh(subject)
    return subject
