"""Concept identity, and what happens to a prerequisite that leaves the subject (S24).

A `KC` belongs to one topic of one subject, so the same concept taught in two subjects was two
unrelated ids with nothing recording that they were about the same thing. These cover the
identity, the deliberate limit on what it is allowed to imply, and the cross-subject
prerequisite policy — which was not a policy at all until now: the edge reached the topological
sort and raised, so a plan was not weakened, it was not produced.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.lesson_plan import DETOUR_EXTERNAL, Edge, prerequisite_closure, topo_sort
from app.llm.registry import fake_llm_client
from app.models.knowledge import KC, Concept, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.services import knowledge as svc
from app.services import lesson_plan as plan_svc

# The canonical form has to agree between Python and migration 0045's SQL backfill; these are
# the cases that separate them if either drifts.
CANONICAL_CASES = [
    ("Derivatives", "derivatives"),
    ("  The Chain Rule  ", "the-chain-rule"),
    ("L'Hôpital's rule", "lhpitals-rule"),
    ("Big-O notation", "bigo-notation"),
    ("Integration by parts!", "integration-by-parts"),
    ("snake_case name", "snakecase-name"),
    ("???", ""),
    ("", ""),
]


async def _subject(session: AsyncSession, name: str) -> Subject:
    subject = Subject(slug=f"{svc.concept_key(name)}-{uuid.uuid4().hex[:6]}", name=name)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="Topic")
    session.add(topic)
    await session.flush()
    return subject


async def _kc(session: AsyncSession, subject: Subject, name: str) -> KC:
    topic = await session.scalar(select(Topic).where(Topic.subject_id == subject.id))
    assert topic is not None
    concept = await svc.resolve_concept(session, name)
    kc = KC(
        topic_id=topic.id,
        slug=f"{svc.concept_key(name) or 'kc'}-{uuid.uuid4().hex[:6]}",
        name=name,
        concept_id=concept.id if concept else None,
    )
    session.add(kc)
    await session.flush()
    return kc


# --- identity -----------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected"), CANONICAL_CASES)
def test_the_canonical_key_ignores_case_and_punctuation(name: str, expected: str) -> None:
    assert svc.concept_key(name) == expected


async def test_the_sql_backfill_canonicalises_exactly_as_the_code_does(
    db_session: AsyncSession,
) -> None:
    """A backfill that disagreed with the code would split every concept it touched: the
    migrated rows would carry one key and every KC created afterwards another."""
    sql = text(
        r"SELECT trim(both '-' from regexp_replace("
        r"regexp_replace(lower(:name), '[^a-z0-9\s]', '', 'g'), '\s+', '-', 'g'))"
    )
    for name, expected in CANONICAL_CASES:
        from_sql = await db_session.scalar(sql, {"name": name})
        assert from_sql == expected, f"SQL and Python disagree on {name!r}"
        assert from_sql == svc.concept_key(name)


async def test_the_same_concept_in_two_subjects_is_one_concept(db_session: AsyncSession) -> None:
    calculus = await _subject(db_session, "Calculus")
    physics = await _subject(db_session, "Physics")

    here = await _kc(db_session, calculus, "Derivatives")
    there = await _kc(db_session, physics, "derivatives")

    assert here.concept_id is not None
    assert here.concept_id == there.concept_id
    assert here.id != there.id, "one concept, two presentations — not one KC"


async def test_a_name_that_canonicalises_to_nothing_gets_no_concept(
    db_session: AsyncSession,
) -> None:
    """Giving every unnameable KC the same empty-string concept would assert they are all the
    same thing, which is the one answer that is certainly wrong."""
    subject = await _subject(db_session, "Oddities")

    kc = await _kc(db_session, subject, "???")

    assert kc.concept_id is None
    assert await svc.resolve_concept(db_session, "!!!") is None


async def test_resolving_the_same_name_twice_does_not_make_a_second_concept(
    db_session: AsyncSession,
) -> None:
    first = await svc.resolve_concept(db_session, "Eigenvalues")
    second = await svc.resolve_concept(db_session, "EIGENVALUES")

    assert first is not None and second is not None
    assert first.id == second.id
    rows = (await db_session.scalars(select(Concept).where(Concept.key == "eigenvalues"))).all()
    assert len(rows) == 1


async def test_a_committed_curriculum_gives_every_component_an_identity(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    result = await svc.create_subject_with_graph(
        db_session,
        "Thermodynamics",
        None,
        [{"name": "Basics", "kcs": [{"name": "Entropy"}, {"name": "Enthalpy"}]}],
        None,
        api_learner.id,
    )

    kcs = await svc.list_kcs_for_subject(db_session, result.subject.id)
    assert {kc.name for kc in kcs} == {"Entropy", "Enthalpy"}
    assert all(kc.concept_id is not None for kc in kcs)
    assert len({kc.concept_id for kc in kcs}) == 2, "two names, two concepts"


async def test_one_curriculum_naming_a_concept_twice_reaches_one_concept_row(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """Resolution is per KC rather than a batch afterwards, so a payload repeating a name
    inside itself cannot race into two rows for one key."""
    result = await svc.create_subject_with_graph(
        db_session,
        "Statistics",
        None,
        [
            {"name": "Descriptive", "kcs": [{"name": "Variance"}]},
            {"name": "Inference", "kcs": [{"name": "variance"}]},
        ],
        None,
        api_learner.id,
    )

    kcs = await svc.list_kcs_for_subject(db_session, result.subject.id)
    assert len(kcs) == 2
    assert len({kc.concept_id for kc in kcs}) == 1


# --- the prerequisite that leaves the subject ---------------------------------------------


def test_a_prerequisite_outside_the_tiebreak_no_longer_stops_the_sort() -> None:
    """The unit, and the actual defect: `tiebreak` holds this subject's KCs as integers, and
    the fallback returned the bare UUID — so sorting the ready list compared an int with a
    UUID and raised. A cross-subject prerequisite did not weaken the ordering, it meant no
    plan at all."""
    local = [uuid.uuid4() for _ in range(3)]
    foreign = uuid.uuid4()
    tiebreak = {kc: i for i, kc in enumerate(local)}
    edges = [Edge(prereq_kc_id=foreign, kc_id=local[0])]

    closure = prerequisite_closure(set(local), edges)
    assert foreign in closure

    ordered = topo_sort(closure, edges, tiebreak)

    assert set(ordered) == closure
    assert ordered.index(foreign) < ordered.index(local[0]), "the edge is still honoured"
    assert ordered[: len(local) - 1] == local[1:], "known KCs keep their order, unknown sorts after"


async def test_a_cross_subject_prerequisite_is_reported_with_the_subject_that_owns_it(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    calculus = await _subject(db_session, "Calculus")
    physics = await _subject(db_session, "Physics")
    limits = await _kc(db_session, calculus, "Limits")
    velocity = await _kc(db_session, physics, "Instantaneous velocity")
    db_session.add(KCEdge(prereq_kc_id=limits.id, kc_id=velocity.id))
    await db_session.flush()

    report = await svc.cross_subject_prerequisites(
        db_session, physics.id, learner_id=api_learner.id
    )

    assert len(report) == 1
    entry = report[0]
    assert entry.prereq_kc_id == limits.id
    assert entry.prereq_subject_id == calculus.id
    assert entry.prereq_subject_name == "Calculus"
    assert entry.kc_id == velocity.id
    assert entry.met_elsewhere is False


async def test_a_prerequisite_inside_the_subject_is_not_reported(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The ordinary case has to stay empty, or the report is noise nobody reads."""
    physics = await _subject(db_session, "Mechanics")
    first = await _kc(db_session, physics, "Newton's laws")
    second = await _kc(db_session, physics, "Momentum")
    db_session.add(KCEdge(prereq_kc_id=first.id, kc_id=second.id))
    await db_session.flush()

    assert await svc.cross_subject_prerequisites(db_session, physics.id) == []


async def test_a_concept_the_learner_has_met_elsewhere_is_flagged_but_not_called_mastered(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The line this item turns on. Sharing a canonical name is evidence about names, so the
    report says the learner has met the concept and stops there — a system that read it as
    transferred mastery would stop teaching something they have never seen."""
    calculus = await _subject(db_session, "Calculus")
    physics = await _subject(db_session, "Physics")
    economics = await _subject(db_session, "Economics")

    prereq = await _kc(db_session, calculus, "Derivatives")
    dependent = await _kc(db_session, physics, "Instantaneous velocity")
    elsewhere = await _kc(db_session, economics, "Derivatives")
    db_session.add(KCEdge(prereq_kc_id=prereq.id, kc_id=dependent.id))
    db_session.add(
        LearnerKCState(learner_id=api_learner.id, kc_id=elsewhere.id, ability=2.0, uncertainty=0.1)
    )
    await db_session.flush()

    report = await svc.cross_subject_prerequisites(
        db_session, physics.id, learner_id=api_learner.id
    )

    assert [e.met_elsewhere for e in report] == [True]
    # And the estimate stays where the evidence was: nothing was written against the
    # prerequisite itself on the strength of a shared name.
    assert (
        await db_session.scalar(select(LearnerKCState).where(LearnerKCState.kc_id == prereq.id))
    ) is None


async def test_evidence_on_the_prerequisite_itself_is_not_meeting_it_elsewhere(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """Otherwise every prerequisite the learner has so much as been estimated on reports as
    already met, and the flag stops distinguishing anything."""
    calculus = await _subject(db_session, "Calculus")
    physics = await _subject(db_session, "Physics")
    prereq = await _kc(db_session, calculus, "Derivatives")
    dependent = await _kc(db_session, physics, "Instantaneous velocity")
    db_session.add(KCEdge(prereq_kc_id=prereq.id, kc_id=dependent.id))
    db_session.add(
        LearnerKCState(learner_id=api_learner.id, kc_id=prereq.id, ability=2.0, uncertainty=0.1)
    )
    await db_session.flush()

    report = await svc.cross_subject_prerequisites(
        db_session, physics.id, learner_id=api_learner.id
    )

    assert [e.met_elsewhere for e in report] == [False]


async def test_plan_generation_survives_a_prerequisite_that_leaves_the_subject(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The call site, not the sort. Covering `topo_sort` directly and its one caller not at all
    is the recurring defect in this repository, and it is what this test exists against: the
    unit test above passes on a planner that never reaches the fixed code.

    The foreign prerequisite cannot be ordered *inside* this plan's local closure/topo-sort,
    and the plan is produced rather than the whole request raising — but it is not simply
    absent (S24): with nothing linking it to a component here, it is planned separately as an
    external detour ahead of the local step it blocks."""
    calculus = await _subject(db_session, "Calculus")
    physics = await _subject(db_session, "Physics")
    limits = await _kc(db_session, calculus, "Limits")
    velocity = await _kc(db_session, physics, "Instantaneous velocity")
    momentum = await _kc(db_session, physics, "Momentum")
    db_session.add(KCEdge(prereq_kc_id=limits.id, kc_id=velocity.id))
    db_session.add(KCEdge(prereq_kc_id=velocity.id, kc_id=momentum.id))
    await db_session.commit()

    plan = await plan_svc.generate_lesson_plan(
        db_session,
        fake_llm_client(),
        learner_id=api_learner.id,
        subject_id=physics.id,
        goal=None,
    )

    new_step_ids = [step["kc_id"] for step in plan.steps if step["step_type"] == "new"]
    assert new_step_ids == [str(velocity.id), str(momentum.id)], "the local ordering still holds"
    limits_step = next(s for s in plan.steps if s["kc_id"] == str(limits.id))
    assert limits_step["step_type"] == "detour" and limits_step["detour_reason"] == DETOUR_EXTERNAL


async def test_only_the_prerequisite_that_leaves_the_subject_is_reported(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """A subject with both kinds of edge. Reporting the internal one too would bury the
    actionable entry in the ordinary ones — and a subject with no foreign edges returns early,
    so a test with only internal edges never reaches the line that decides this."""
    calculus = await _subject(db_session, "Calculus")
    physics = await _subject(db_session, "Physics")
    limits = await _kc(db_session, calculus, "Limits")
    velocity = await _kc(db_session, physics, "Instantaneous velocity")
    momentum = await _kc(db_session, physics, "Momentum")
    db_session.add(KCEdge(prereq_kc_id=limits.id, kc_id=velocity.id))
    db_session.add(KCEdge(prereq_kc_id=velocity.id, kc_id=momentum.id))
    await db_session.flush()

    report = await svc.cross_subject_prerequisites(
        db_session, physics.id, learner_id=api_learner.id
    )

    assert [(e.prereq_kc_id, e.kc_id) for e in report] == [(limits.id, velocity.id)]
