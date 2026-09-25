"""Concept links: which pairs are candidates, and how a link comes into effect (S24)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.learning import mastery
from app.models.knowledge import KC, Concept, ConceptLinkDecision, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.services import concept_links as svc


async def _subject(session, *, name, owner=None) -> Subject:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name=name, owner_learner_id=owner)
    session.add(subject)
    await session.flush()
    return subject


async def _kc(session, subject: Subject, name: str, concept: Concept | None) -> KC:
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(
        topic_id=topic.id,
        slug=f"k-{uuid.uuid4().hex[:8]}",
        name=name,
        concept_id=concept.id if concept else None,
    )
    session.add(kc)
    await session.flush()
    return kc


async def _concept(session, key: str) -> Concept:
    concept = Concept(key=f"{key}-{uuid.uuid4().hex[:6]}", name=key)
    session.add(concept)
    await session.flush()
    return concept


async def _learner(session) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_two_curated_presentations_of_one_concept_become_a_curated_candidate(db_session):
    concept = await _concept(db_session, "derivatives")
    calc = await _subject(db_session, name="Calculus")
    phys = await _subject(db_session, name="Physics")
    a = await _kc(db_session, calc, "Derivatives", concept)
    b = await _kc(db_session, phys, "Derivatives", concept)
    assert await svc.sync_candidates(db_session, None) == 1
    (link,) = await svc.curated_queue(db_session)
    assert {link.kc_a_id, link.kc_b_id} == {a.id, b.id}
    assert link.kc_a_id < link.kc_b_id
    assert (link.scope, link.owner_learner_id, link.verdict) == ("curated", None, None)
    assert await svc.sync_candidates(db_session, None) == 0  # idempotent


async def test_a_private_pair_belongs_to_its_learner_and_never_spans_two_learners(db_session):
    concept = await _concept(db_session, "vectors")
    one, two = await _learner(db_session), await _learner(db_session)
    mine = await _subject(db_session, name="Mine", owner=one.id)
    theirs = await _subject(db_session, name="Theirs", owner=two.id)
    curated = await _subject(db_session, name="Library")
    await _kc(db_session, mine, "Vectors", concept)
    await _kc(db_session, theirs, "Vectors", concept)
    await _kc(db_session, curated, "Vectors", concept)
    # One's view: mine+library. Two's view: theirs+library. Never mine+theirs.
    assert await svc.sync_candidates(db_session, one.id) == 1
    assert await svc.sync_candidates(db_session, two.id) == 1
    links = await svc.links_for_learner(db_session, one.id)
    assert [(link.scope, link.owner_learner_id) for link in links] == [("private", one.id)]


async def test_components_in_the_same_subject_are_never_candidates(db_session):
    concept = await _concept(db_session, "limits")
    calc = await _subject(db_session, name="Calculus")
    await _kc(db_session, calc, "Limits", concept)
    await _kc(db_session, calc, "Limits", concept)
    assert await svc.sync_candidates(db_session, None) == 0


async def test_a_link_is_in_effect_only_when_endorsed_and_accepted(db_session):
    learner = await _learner(db_session)
    concept = await _concept(db_session, "sets")
    a = await _kc(db_session, await _subject(db_session, name="A"), "Sets", concept)
    b = await _kc(db_session, await _subject(db_session, name="B"), "Sets", concept)
    await svc.sync_candidates(db_session, None)
    (link,) = await svc.curated_queue(db_session)
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {}
    db_session.add(ConceptLinkDecision(learner_id=learner.id, link_id=link.id, decision="accepted"))
    await db_session.flush()
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {}  # not endorsed
    admin = await _learner(db_session)
    await svc.set_admin_verdict(db_session, link.id, admin.id, endorse=True, reason="Same idea.")
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {a.id: {b.id}}
    assert await svc.links_in_effect(db_session, learner.id, [b.id]) == {b.id: {a.id}}


async def test_an_admin_decides_a_curated_link_once_and_never_a_private_one(db_session):
    learner, admin = await _learner(db_session), await _learner(db_session)
    concept = await _concept(db_session, "graphs")
    await _kc(db_session, await _subject(db_session, name="A"), "Graphs", concept)
    await _kc(db_session, await _subject(db_session, name="B", owner=learner.id), "Graphs", concept)
    await _kc(db_session, await _subject(db_session, name="C"), "Graphs", concept)
    await svc.sync_candidates(db_session, learner.id)
    private = next(
        link
        for link in await svc.links_for_learner(db_session, learner.id)
        if link.scope == "private"
    )
    curated = (await svc.curated_queue(db_session))[0]
    with pytest.raises(svc.LinkNotFound):
        await svc.set_admin_verdict(db_session, private.id, admin.id, endorse=True, reason="x")
    decided = await svc.set_admin_verdict(
        db_session, curated.id, admin.id, endorse=False, reason="Different use."
    )
    assert (decided.verdict, decided.endorsed_by, decided.decided_by_admin_id) == (
        "rejected",
        "admin",
        admin.id,
    )
    with pytest.raises(svc.LinkAlreadyDecided):
        await svc.set_admin_verdict(db_session, curated.id, admin.id, endorse=True, reason="y")


async def _endorsed_curated(db_session):
    concept = await _concept(db_session, "matrices")
    a = await _kc(
        db_session, await _subject(db_session, name="Linear Algebra"), "Matrices", concept
    )
    b = await _kc(db_session, await _subject(db_session, name="Graphics"), "Matrices", concept)
    await svc.sync_candidates(db_session, None)
    (link,) = await svc.curated_queue(db_session)
    admin = await _learner(db_session)
    await svc.set_admin_verdict(db_session, link.id, admin.id, endorse=True, reason="Same object.")
    return link, a, b


async def test_accepting_seeds_the_unmeasured_side(db_session):
    learner = await _learner(db_session)
    link, a, b = await _endorsed_curated(db_session)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=a.id,
            ability=2.0,
            uncertainty=0.3,
            last_seen_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    await svc.decide(db_session, learner.id, link.id, "accept")
    assert await mastery.provisional_kc_ids(db_session, learner.id, [a.id, b.id]) == {b.id}


async def test_decisions_follow_from_each_other(db_session):
    learner = await _learner(db_session)
    link, _a, _b = await _endorsed_curated(db_session)
    with pytest.raises(svc.LinkConflict):
        await svc.decide(db_session, learner.id, link.id, "revoke")  # nothing to revoke
    await svc.decide(db_session, learner.id, link.id, "decline")
    assert (
        await svc.decide(db_session, learner.id, link.id, "decline")
    ).decision == "declined"  # idempotent
    with pytest.raises(svc.LinkConflict):
        await svc.decide(db_session, learner.id, link.id, "accept")  # a decline is final


async def test_revoking_withdraws_an_unanswered_head_start(db_session):
    learner = await _learner(db_session)
    link, a, b = await _endorsed_curated(db_session)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=a.id,
            ability=2.0,
            uncertainty=0.3,
            last_seen_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    await svc.decide(db_session, learner.id, link.id, "accept")
    await svc.decide(db_session, learner.id, link.id, "revoke")
    assert await mastery.provisional_kc_ids(db_session, learner.id, [b.id]) == set()
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {}


async def test_an_unendorsed_or_invisible_link_cannot_be_decided(db_session):
    learner, stranger = await _learner(db_session), await _learner(db_session)
    concept = await _concept(db_session, "trees")
    await _kc(
        db_session, await _subject(db_session, name="Mine", owner=stranger.id), "Trees", concept
    )
    await _kc(db_session, await _subject(db_session, name="Lib"), "Trees", concept)
    await svc.sync_candidates(db_session, stranger.id)
    (theirs,) = await svc.links_for_learner(db_session, stranger.id)
    theirs.verdict = "endorsed"
    await db_session.flush()
    with pytest.raises(svc.LinkNotFound):
        await svc.decide(db_session, learner.id, theirs.id, "accept")
    link, _a, _b = await _endorsed_curated(db_session)
    link.verdict = None
    await db_session.flush()
    with pytest.raises(svc.LinkNotFound):
        await svc.decide(db_session, learner.id, link.id, "accept")


async def test_suggestions_list_endorsed_undecided_and_accepted_links_only(db_session):
    learner = await _learner(db_session)
    link, _a, _b = await _endorsed_curated(db_session)
    (s,) = await svc.suggestions(db_session, learner.id)
    assert (s.link_id, s.decision, s.reason) == (link.id, None, "Same object.")
    assert {s.a.subject_name, s.b.subject_name} == {"Linear Algebra", "Graphics"}
    await svc.decide(db_session, learner.id, link.id, "accept")
    assert [x.decision for x in await svc.suggestions(db_session, learner.id)] == ["accepted"]
    await svc.decide(db_session, learner.id, link.id, "revoke")
    assert [x.decision for x in await svc.suggestions(db_session, learner.id)] == [None]


async def test_revoking_one_link_reseeds_the_target_from_another_still_in_effect(db_session):
    """Controller ruling (S24): revoking one head start hands the target to any other link
    still in effect, rather than leaving it at the unknown prior when a second source exists."""
    learner, admin = await _learner(db_session), await _learner(db_session)
    concept = await _concept(db_session, "limits")
    a = await _kc(db_session, await _subject(db_session, name="A"), "Limits", concept)
    b = await _kc(db_session, await _subject(db_session, name="B"), "Limits", concept)
    t = await _kc(db_session, await _subject(db_session, name="T"), "Limits", concept)
    await svc.sync_candidates(db_session, None)
    for link in await svc.curated_queue(db_session):
        await svc.set_admin_verdict(
            db_session, link.id, admin.id, endorse=True, reason="Same idea."
        )
    links = {
        frozenset((link.kc_a_id, link.kc_b_id)): link
        for link in await svc.links_for_learner(db_session, learner.id)
    }
    link_at, link_bt = links[frozenset((a.id, t.id))], links[frozenset((b.id, t.id))]
    now = datetime.now(UTC)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id, kc_id=a.id, ability=2.0, uncertainty=0.3, last_seen_at=now
        )
    )
    db_session.add(
        LearnerKCState(
            learner_id=learner.id, kc_id=b.id, ability=1.0, uncertainty=0.3, last_seen_at=now
        )
    )
    await db_session.flush()
    await svc.decide(db_session, learner.id, link_at.id, "accept")
    await svc.decide(db_session, learner.id, link_bt.id, "accept")
    state = await db_session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == t.id
        )
    )
    assert state.transferred_from_kc_id == a.id  # the stronger source won on accept

    await svc.decide(db_session, learner.id, link_at.id, "revoke")

    assert await mastery.provisional_kc_ids(db_session, learner.id, [t.id]) == {t.id}
    await db_session.refresh(state)
    assert state.transferred_from_kc_id == b.id
