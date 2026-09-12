"""A misconception that keeps coming back reads as a pattern, not as five unrelated slips (S09).

A diagnosis was a property of one attempt and nothing joined them up, so one persistent wrong
belief and five different bad moments produced identical instructions — and they need opposite
ones. What this branches on is a *count of independent observations*, deliberately never the
model's own confidence, which is uncalibrated and may gate nothing.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import feedback, mastery
from app.learning.diagnosis import Diagnosis, FailureKind
from app.learning.grading import GradeResult
from app.learning.mastery import Observation
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner


async def _learner_and_kc(session: AsyncSession) -> tuple[Learner, KC]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="k", name="Velocity")
    session.add(kc)
    await session.flush()
    return learner, kc


async def _observe(
    session: AsyncSession, learner: Learner, kc: KC, score: float, kind: FailureKind | None
) -> None:
    diagnosis = (
        {kc.id: {"kind": kind.value, "confidence": 0.9, "detail": "", "prerequisite": ""}}
        if kind is not None
        else None
    )
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id, kc_weights={kc.id: 1.0}, score=score, kc_diagnoses=diagnosis
        ),
    )
    await session.flush()


# --- the aggregation itself ---------------------------------------------------------------


async def test_the_same_kind_across_attempts_is_counted(db_session: AsyncSession) -> None:
    learner, kc = await _learner_and_kc(db_session)
    await _observe(db_session, learner, kc, 0.2, FailureKind.CONCEPTUAL)
    await _observe(db_session, learner, kc, 0.3, FailureKind.CONCEPTUAL)
    await _observe(db_session, learner, kc, 0.1, FailureKind.NOTATION)

    counts = await mastery.prior_failure_kinds(db_session, learner.id, [kc.id])
    assert counts[kc.id][FailureKind.CONCEPTUAL] == 2
    assert counts[kc.id][FailureKind.NOTATION] == 1


async def test_different_kinds_do_not_add_up_to_a_pattern(db_session: AsyncSession) -> None:
    """Three different failures is a learner having a bad run, not one wrong belief. The whole
    point of the vocabulary is that these are not interchangeable."""
    learner, kc = await _learner_and_kc(db_session)
    await _observe(db_session, learner, kc, 0.2, FailureKind.CONCEPTUAL)
    await _observe(db_session, learner, kc, 0.3, FailureKind.NOTATION)
    await _observe(db_session, learner, kc, 0.1, FailureKind.PROCEDURAL)

    counts = await mastery.prior_failure_kinds(db_session, learner.id, [kc.id])
    assert max(counts[kc.id].values()) == 1


async def test_a_clean_answer_is_not_a_failure_kind(db_session: AsyncSession) -> None:
    """``none`` and ``incomplete`` are excluded: one says nothing went wrong, the other says
    nothing was shown either way, and neither is a recurring mistake."""
    learner, kc = await _learner_and_kc(db_session)
    await _observe(db_session, learner, kc, 1.0, FailureKind.NONE)
    await _observe(db_session, learner, kc, 0.0, FailureKind.INCOMPLETE)

    counts = await mastery.prior_failure_kinds(db_session, learner.id, [kc.id])
    assert counts == {}


async def test_another_learners_history_is_not_counted(db_session: AsyncSession) -> None:
    learner, kc = await _learner_and_kc(db_session)
    other = Learner(handle=f"o-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    await _observe(db_session, other, kc, 0.2, FailureKind.CONCEPTUAL)
    await _observe(db_session, other, kc, 0.2, FailureKind.CONCEPTUAL)

    counts = await mastery.prior_failure_kinds(db_session, learner.id, [kc.id])
    assert counts == {}


async def test_no_components_asks_nothing(db_session: AsyncSession) -> None:
    learner, _kc = await _learner_and_kc(db_session)
    assert await mastery.prior_failure_kinds(db_session, learner.id, []) == {}


# --- what it changes about the teaching -----------------------------------------------------


def _result(kc_id: uuid.UUID, kind: FailureKind) -> GradeResult:
    return GradeResult(
        score=0.3,
        correct=False,
        detail={},
        diagnoses={kc_id: Diagnosis(kind=kind, confidence=0.8)},
    )


def test_a_first_mistake_gets_the_ordinary_repair() -> None:
    kc_id = uuid.uuid4()
    notes = feedback.diagnosis_notes(
        _result(kc_id, FailureKind.CONCEPTUAL), {kc_id: "Velocity"}, prior_kinds={}
    )
    assert any("Re-teach the idea itself" in note for note in notes)
    assert not any("time Velocity has gone wrong" in note for note in notes)


def test_a_recurring_mistake_stops_being_told_to_re_explain() -> None:
    """The escalation *replaces* the repair rather than being appended to it: "re-teach the
    idea from a different angle" is precisely the advice that has already failed twice."""
    kc_id = uuid.uuid4()
    notes = feedback.diagnosis_notes(
        _result(kc_id, FailureKind.CONCEPTUAL),
        {kc_id: "Velocity"},
        prior_kinds={kc_id: {FailureKind.CONCEPTUAL: 2}},
    )
    assert any("3rd time Velocity has gone wrong the same way" in note for note in notes)
    assert not any("Re-teach the idea itself" in note for note in notes)


def test_a_different_kind_recurring_does_not_escalate_this_one() -> None:
    kc_id = uuid.uuid4()
    notes = feedback.diagnosis_notes(
        _result(kc_id, FailureKind.CONCEPTUAL),
        {kc_id: "Velocity"},
        prior_kinds={kc_id: {FailureKind.NOTATION: 5}},
    )
    assert any("Re-teach the idea itself" in note for note in notes)


def test_one_repeat_is_still_a_coincidence() -> None:
    kc_id = uuid.uuid4()
    notes = feedback.diagnosis_notes(
        _result(kc_id, FailureKind.PROCEDURAL),
        {kc_id: "Velocity"},
        prior_kinds={kc_id: {FailureKind.PROCEDURAL: feedback.RECURRENCE_MIN - 1}},
    )
    assert not any("gone wrong the same way" in note for note in notes)


def test_without_history_nothing_escalates() -> None:
    """The default has to be the cautious one: a flow that has not looked up the history must
    not accuse a learner of repeating themselves."""
    kc_id = uuid.uuid4()
    notes = feedback.diagnosis_notes(_result(kc_id, FailureKind.CONCEPTUAL), {kc_id: "Velocity"})
    assert any("Re-teach the idea itself" in note for note in notes)
