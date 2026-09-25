"""Concept links between presentations in different subjects (S24).

A pair of KCs sharing a concept key (``knowledge.concept_key``) is a *candidate* — the name
only makes the pair worth a look. A link comes into effect for a learner only when it has been
**endorsed** (an administrator for a curated pair; the LLM judge for a pair touching the
learner's own material) **and** that learner has **accepted** it. ``links_in_effect`` is the
one reading of that rule; nothing else re-derives it.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import combinations

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, ConceptLink, ConceptLinkDecision, Subject, Topic

CURATED, PRIVATE = "curated", "private"
ENDORSED, REJECTED = "endorsed", "rejected"
ACCEPTED, DECLINED, REVOKED = "accepted", "declined", "revoked"


class LinkNotFound(Exception):
    """No such link — or not one this caller may see or act on. One answer for both."""


class LinkAlreadyDecided(Exception):
    """An endorser has already ruled on this link."""


class LinkConflict(Exception):
    """The learner's decision does not follow from the one they already made."""


async def sync_candidates(session: AsyncSession, learner_id: uuid.UUID | None) -> int:
    """Record every candidate pair visible from ``learner_id``'s point of view; return how many
    were new.

    ``None`` is the curated library alone. A learner sees the library plus their own subjects,
    so their candidates are library-own and own-own pairs; library-library pairs are recorded
    too (as curated) since they are candidates for everyone. A pair only ever spans subjects one
    learner can see — two learners' private material is never paired.
    """
    visible = Subject.owner_learner_id.is_(None)
    if learner_id is not None:
        visible = or_(visible, Subject.owner_learner_id == learner_id)
    rows = (
        await session.execute(
            select(KC.id, KC.concept_id, Subject.id, Subject.owner_learner_id)
            .join(Topic, KC.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            .where(KC.concept_id.is_not(None), visible)
        )
    ).all()
    by_concept: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]]] = defaultdict(
        list
    )
    for kc_id, concept_id, subject_id, owner in rows:
        by_concept[concept_id].append((kc_id, subject_id, owner))
    values = []
    for members in by_concept.values():
        for (kc1, s1, o1), (kc2, s2, o2) in combinations(members, 2):
            if s1 == s2:
                continue
            a, b = sorted((kc1, kc2))
            curated = o1 is None and o2 is None
            values.append(
                {
                    "id": uuid.uuid4(),
                    "kc_a_id": a,
                    "kc_b_id": b,
                    "scope": CURATED if curated else PRIVATE,
                    "owner_learner_id": None if curated else (o1 or o2),
                }
            )
    if not values:
        return 0
    result = await session.execute(
        pg_insert(ConceptLink)
        .values(values)
        .on_conflict_do_nothing(index_elements=["kc_a_id", "kc_b_id"])
        .returning(ConceptLink.id)
    )
    return len(result.all())


def _visible_link(learner_id: uuid.UUID):
    """A link this learner may see: curated, or private and theirs."""
    return or_(ConceptLink.scope == CURATED, ConceptLink.owner_learner_id == learner_id)


async def links_for_learner(session: AsyncSession, learner_id: uuid.UUID) -> list[ConceptLink]:
    return list(
        await session.scalars(
            select(ConceptLink).where(_visible_link(learner_id)).order_by(ConceptLink.created_at)
        )
    )


async def links_in_effect(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """For each of ``kc_ids`` with at least one link in effect for this learner, the KCs it is
    linked to. In effect means endorsed **and** accepted by this learner — the whole rule."""
    ids = list(kc_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ConceptLink.kc_a_id, ConceptLink.kc_b_id)
            .join(ConceptLinkDecision, ConceptLinkDecision.link_id == ConceptLink.id)
            .where(
                ConceptLink.verdict == ENDORSED,
                ConceptLinkDecision.learner_id == learner_id,
                ConceptLinkDecision.decision == ACCEPTED,
                or_(ConceptLink.kc_a_id.in_(ids), ConceptLink.kc_b_id.in_(ids)),
            )
        )
    ).all()
    wanted = set(ids)
    out: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for a, b in rows:
        if a in wanted:
            out[a].add(b)
        if b in wanted:
            out[b].add(a)
    return dict(out)


async def curated_queue(session: AsyncSession) -> list[ConceptLink]:
    """Curated links, undecided first, oldest first within each — the order a reviewer works in."""
    return list(
        await session.scalars(
            select(ConceptLink)
            .where(ConceptLink.scope == CURATED)
            .order_by(ConceptLink.verdict.is_not(None), ConceptLink.created_at)
        )
    )


async def set_admin_verdict(
    session: AsyncSession, link_id: uuid.UUID, admin_id: uuid.UUID, *, endorse: bool, reason: str
) -> ConceptLink:
    """An administrator's ruling on a curated link. Once only; private links are the judge's."""
    link = await session.get(ConceptLink, link_id)
    if link is None or link.scope != CURATED:
        raise LinkNotFound(str(link_id))
    if link.verdict is not None:
        raise LinkAlreadyDecided(str(link_id))
    link.verdict = ENDORSED if endorse else REJECTED
    link.endorsed_by = "admin"
    link.decided_by_admin_id = admin_id
    link.reason = reason
    link.decided_at = datetime.now(UTC)
    await session.flush()
    return link
