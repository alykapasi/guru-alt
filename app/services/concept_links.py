"""Concept links between presentations in different subjects (S24).

A pair of KCs sharing a concept key (``knowledge.concept_key``) is a *candidate* — the name
only makes the pair worth a look. A link comes into effect for a learner only when it has been
**endorsed** (an administrator for a curated pair; the LLM judge for a pair touching the
learner's own material) **and** that learner has **accepted** it. ``links_in_effect`` is the
one reading of that rule; nothing else re-derives it.
"""

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from typing import Literal, cast

import structlog
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import link_judge, mastery
from app.llm import LLMClient
from app.models.knowledge import KC, ConceptLink, ConceptLinkDecision, KCEdge, Subject, Topic
from app.schemas.concept_links import ConceptLinkReviewRead
from app.services.llm_log import log_llm_call

log = structlog.get_logger(__name__)

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


async def _side(
    session: AsyncSession, kc_id: uuid.UUID, visible_subjects: set[uuid.UUID]
) -> link_judge.Side | None:
    """What the judge is told about one side — its own subject's facts only, and neighbours
    only from subjects the learner can see."""
    row = (
        await session.execute(
            select(KC, Topic, Subject)
            .join(Topic, KC.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            .where(KC.id == kc_id)
        )
    ).first()
    if row is None:
        return None
    kc, topic, subject = row

    async def neighbours(near, far) -> list[str]:
        names = await session.scalars(
            select(KC.name)
            .join(KCEdge, far == KC.id)
            .join(Topic, KC.topic_id == Topic.id)
            .where(near == kc_id, Topic.subject_id.in_(visible_subjects))
            .order_by(KC.name)
        )
        return list(names)

    return link_judge.Side(
        kc_name=kc.name,
        description=kc.description,
        topic_name=topic.name,
        subject_name=subject.name,
        prerequisites=await neighbours(KCEdge.kc_id, KCEdge.prereq_kc_id),
        dependents=await neighbours(KCEdge.prereq_kc_id, KCEdge.kc_id),
    )


async def judge_pending(session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID) -> int:
    """Judge this learner's unjudged private candidates; return how many got a verdict.

    Commits after each verdict, so a run that dies halfway keeps what it decided. A pair the
    judge could not decide stays unjudged for the next run — never recorded as a rejection.
    """
    await sync_candidates(session, learner_id)
    await session.commit()
    visible_subjects = set(
        await session.scalars(
            select(Subject.id).where(
                or_(Subject.owner_learner_id.is_(None), Subject.owner_learner_id == learner_id)
            )
        )
    )
    pending = list(
        await session.scalars(
            select(ConceptLink).where(
                ConceptLink.scope == PRIVATE,
                ConceptLink.owner_learner_id == learner_id,
                ConceptLink.verdict.is_(None),
            )
        )
    )
    decided = 0
    for link in pending:
        a = await _side(session, link.kc_a_id, visible_subjects)
        b = await _side(session, link.kc_b_id, visible_subjects)
        if a is None or b is None:
            continue
        verdict, usage = await link_judge.judge_pair(llm, a, b)
        if usage.input_tokens or usage.output_tokens:
            await log_llm_call(
                learner_id=learner_id,
                role=link_judge.JUDGE_ROLE.value,
                spec=llm.spec(link_judge.JUDGE_ROLE),
                usage=usage,
            )
        if verdict is None:
            log.warning("concept_links.judge_undecided", link_id=str(link.id))
            continue
        link.verdict = ENDORSED if verdict.endorse else REJECTED
        link.endorsed_by = "judge"
        link.reason = verdict.reason
        link.decided_at = datetime.now(UTC)
        await session.commit()
        decided += 1
    return decided


async def dispatch_judge(
    enqueue: Callable[[uuid.UUID], Awaitable[None]], learner_id: uuid.UUID
) -> bool:
    """Best-effort enqueue of concept-link judging for an already-committed subject.

    Mirrors ``ingestion.dispatch``: by the time this is called the subject is already durable,
    so a queue failure here must not fail the caller — there is no lie to avoid telling and
    nothing to retry. Unlike ingestion there is no reconciliation sweep; instead, a lost enqueue
    only delays suggestions until the learner's next commit, when ``judge_pending`` re-syncs
    candidates from scratch and picks up whatever this run missed.
    """
    try:
        await enqueue(learner_id)
        return True
    except Exception as exc:
        log.warning(
            "concept_links.judge_enqueue_failed", learner_id=str(learner_id), error=str(exc)
        )
        return False


@dataclass(frozen=True)
class LinkSide:
    kc_id: uuid.UUID
    kc_name: str
    subject_id: uuid.UUID
    subject_name: str


@dataclass(frozen=True)
class Suggestion:
    link_id: uuid.UUID
    reason: str | None
    endorsed_by: str | None
    decision: str | None  # None (undecided, or revoked) | "accepted"
    a: LinkSide
    b: LinkSide


async def _decidable(
    session: AsyncSession, learner_id: uuid.UUID, link_id: uuid.UUID
) -> ConceptLink:
    """The link, if this learner may decide it: endorsed, and visible to them. One answer
    (``LinkNotFound``) for every other case, so a caller cannot probe for other learners' pairs."""
    link = await session.scalar(
        select(ConceptLink).where(ConceptLink.id == link_id, _visible_link(learner_id))
    )
    if link is None or link.verdict != ENDORSED:
        raise LinkNotFound(str(link_id))
    return link


async def _sides_for(session: AsyncSession, kc_ids: set[uuid.UUID]) -> dict[uuid.UUID, LinkSide]:
    """Each of ``kc_ids``, with its own KC and subject names, in one query. Shared by every
    reader that turns a link's raw ``kc_a_id``/``kc_b_id`` into a ``LinkSide`` — ``suggestions``,
    ``describe`` — so there is exactly one join to keep in sync with the schema."""
    if not kc_ids:
        return {}
    return {
        kc.id: LinkSide(
            kc_id=kc.id, kc_name=kc.name, subject_id=subject.id, subject_name=subject.name
        )
        for kc, subject in (
            await session.execute(
                select(KC, Subject)
                .join(Topic, KC.topic_id == Topic.id)
                .join(Subject, Topic.subject_id == Subject.id)
                .where(KC.id.in_(kc_ids))
            )
        ).all()
    }


async def describe(session: AsyncSession, learner_id: uuid.UUID, link_id: uuid.UUID) -> Suggestion:
    """One decidable link exactly as ``decide`` sees it — endorsed and visible to this learner —
    whatever they have already decided about it (S24).

    Unlike ``suggestions``, which is a *menu* and drops a link the learner declined (it is not
    offered again), this is a *lookup*: the decision route uses it as both its existence probe
    and its response for every decision, including a repeat decline or an attempt that a
    ``LinkConflict`` refuses. Using ``suggestions`` for that turned every decision after a
    decline into a 404, because the very thing a decline does is remove the link from it.
    """
    link = await _decidable(session, learner_id, link_id)
    row = await session.scalar(
        select(ConceptLinkDecision).where(
            ConceptLinkDecision.learner_id == learner_id, ConceptLinkDecision.link_id == link_id
        )
    )
    sides = await _sides_for(session, {link.kc_a_id, link.kc_b_id})
    if link.kc_a_id not in sides or link.kc_b_id not in sides:
        raise LinkNotFound(str(link_id))
    return Suggestion(
        link_id=link.id,
        reason=link.reason,
        endorsed_by=link.endorsed_by,
        decision=ACCEPTED if row is not None and row.decision == ACCEPTED else None,
        a=sides[link.kc_a_id],
        b=sides[link.kc_b_id],
    )


async def _reseed_from_remaining_links(
    session: AsyncSession, learner_id: uuid.UUID, target_kc_id: uuid.UUID
) -> None:
    """After a revoke actually withdraws a head start, hand ``target_kc_id`` to any other link
    still in effect for this learner (S24 controller ruling).

    Reads ``links_in_effect`` after the revoked decision has been flushed, so that link no
    longer counts. ``seed_transfer`` already keeps only the strongest source, so calling it for
    every remaining link is safe even when more than one is in effect.
    """
    in_effect = await links_in_effect(session, learner_id, [target_kc_id])
    for source_kc_id in in_effect.get(target_kc_id, ()):
        a, b = sorted((target_kc_id, source_kc_id))
        other_link = await session.scalar(
            select(ConceptLink).where(ConceptLink.kc_a_id == a, ConceptLink.kc_b_id == b)
        )
        if other_link is not None:
            await mastery.seed_transfer(
                session,
                learner_id,
                target_kc_id=target_kc_id,
                source_kc_id=source_kc_id,
                link_id=other_link.id,
            )


async def decide(
    session: AsyncSession,
    learner_id: uuid.UUID,
    link_id: uuid.UUID,
    decision: Literal["accept", "decline", "revoke"],
) -> ConceptLinkDecision:
    """Record the learner's half of a link and apply it (S24).

    - ``accept`` (from undecided or revoked): seeds each side from the other where one has
      evidence and the other none (``mastery.seed_transfer``).
    - ``decline`` (from undecided): final — the pair is not offered again.
    - ``revoke`` (from accepted): withdraws any head start not yet answered on, and hands the
      target to any other link still in effect for this learner (controller ruling).
    A repeat of the current decision returns it unchanged. Anything else is ``LinkConflict``.
    """
    link = await _decidable(session, learner_id, link_id)
    row = await session.scalar(
        select(ConceptLinkDecision).where(
            ConceptLinkDecision.learner_id == learner_id, ConceptLinkDecision.link_id == link_id
        )
    )
    current = row.decision if row is not None else None
    target = {"accept": ACCEPTED, "decline": DECLINED, "revoke": REVOKED}[decision]
    if current == target:
        assert row is not None  # current == target implies a row
        return row
    allowed = {
        ACCEPTED: (None, REVOKED),
        DECLINED: (None,),
        REVOKED: (ACCEPTED,),
    }[target]
    if current not in allowed:
        raise LinkConflict(f"cannot {decision} a link that is {current or 'undecided'}")
    if row is None:
        row = ConceptLinkDecision(learner_id=learner_id, link_id=link_id, decision=target)
        session.add(row)
    else:
        row.decision, row.decided_at = target, datetime.now(UTC)
    pairs = ((link.kc_a_id, link.kc_b_id), (link.kc_b_id, link.kc_a_id))
    if target == ACCEPTED:
        for target_kc, source_kc in pairs:
            await mastery.seed_transfer(
                session, learner_id, target_kc_id=target_kc, source_kc_id=source_kc, link_id=link.id
            )
    elif target == REVOKED:
        for target_kc, source_kc in pairs:
            withdrawn = await mastery.revoke_transfer(
                session, learner_id, target_kc_id=target_kc, source_kc_id=source_kc, link_id=link.id
            )
            if withdrawn:
                # Flush so the decision row above is already `revoked` when `links_in_effect`
                # (queried inside the reseed) re-reads it — otherwise this same link would
                # still count as in effect and could hand the target right back to itself.
                await session.flush()
                await _reseed_from_remaining_links(session, learner_id, target_kc)
    await session.flush()
    return row


async def suggestions(session: AsyncSession, learner_id: uuid.UUID) -> list[Suggestion]:
    """Endorsed links this learner can see and has not declined — undecided (or revoked) ones
    to accept, accepted ones to revoke. Refreshes candidates first, so a newly endorsed curated
    pair touching a subject the learner can see shows up without waiting for anything."""
    await sync_candidates(session, learner_id)
    decided = {
        d.link_id: d.decision
        for d in await session.scalars(
            select(ConceptLinkDecision).where(ConceptLinkDecision.learner_id == learner_id)
        )
    }
    links = [
        link
        for link in await session.scalars(
            select(ConceptLink)
            .where(_visible_link(learner_id), ConceptLink.verdict == ENDORSED)
            .order_by(ConceptLink.decided_at)
        )
        if decided.get(link.id) != DECLINED
    ]
    kc_ids = {kc for link in links for kc in (link.kc_a_id, link.kc_b_id)}
    sides = await _sides_for(session, kc_ids)
    return [
        Suggestion(
            link_id=link.id,
            reason=link.reason,
            endorsed_by=link.endorsed_by,
            decision=ACCEPTED if decided.get(link.id) == ACCEPTED else None,
            a=sides[link.kc_a_id],
            b=sides[link.kc_b_id],
        )
        for link in links
        if link.kc_a_id in sides and link.kc_b_id in sides
    ]


async def review_rows(session: AsyncSession) -> list[ConceptLinkReviewRead]:
    """Curated links for an administrator to decide, each side's names joined in, in
    ``curated_queue`` order (S24)."""
    links = await curated_queue(session)
    kc_ids = {kc for link in links for kc in (link.kc_a_id, link.kc_b_id)}
    names = (
        {
            kc_id: (kc_name, subject_name)
            for kc_id, kc_name, subject_name in (
                await session.execute(
                    select(KC.id, KC.name, Subject.name)
                    .join(Topic, KC.topic_id == Topic.id)
                    .join(Subject, Topic.subject_id == Subject.id)
                    .where(KC.id.in_(kc_ids))
                )
            ).all()
        }
        if kc_ids
        else {}
    )
    return [
        ConceptLinkReviewRead(
            id=link.id,
            kc_a_id=link.kc_a_id,
            kc_b_id=link.kc_b_id,
            kc_a_name=names[link.kc_a_id][0],
            kc_b_name=names[link.kc_b_id][0],
            subject_a_name=names[link.kc_a_id][1],
            subject_b_name=names[link.kc_b_id][1],
            # `verdict` is `str | None` on the row (curated_queue's own where-clause is what
            # actually narrows it) — cast to the response's closed vocabulary rather than
            # widen the schema to match the column.
            verdict=cast('Literal["endorsed", "rejected"] | None', link.verdict),
            reason=link.reason,
            decided_at=link.decided_at,
        )
        for link in links
        if link.kc_a_id in names and link.kc_b_id in names
    ]
