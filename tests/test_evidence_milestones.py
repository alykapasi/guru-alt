"""The due checks learn which components could be due from milestones, not the event log (S62).

Retention and transfer are still decided by `kc_evidence`. The milestones on learner_kc_state
only record that a component has an unaided answer, has shown retention, or has shown
transfer — each a one-way step — so a check reads evidence only where it could matter.
"""

from datetime import timedelta

from sqlalchemy import select, update

from app.core.config import get_settings
from app.learning import mastery
from app.learning.mastery import Observation
from app.models.assessment import EvidenceKind
from app.models.learning import LearnerKCState
from tests.querycount import count_queries
from tests.test_item_exposure import T0, _item, _kc, _learner


async def _answer(session, learner, kc, item, *, when, hints=None, self_rated=False) -> None:
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            correct=True,
            item_id=item.id,
            hints_used=hints,
            evidence_kind=EvidenceKind.SELF_REPORTED if self_rated else EvidenceKind.DEMONSTRATED,
        ),
        now=when,
    )
    await session.flush()


async def _state(session, learner, kc) -> LearnerKCState:
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == kc.id
        )
    )
    assert state is not None
    return state


async def test_an_unaided_answer_records_its_time(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)

    state = await _state(db_session, learner, kc)
    assert state.unaided_last_at is not None
    assert mastery.naive_utc(state.unaided_last_at) == mastery.naive_utc(T0)
    assert state.retention_shown_at is None
    assert state.evidence_marked_at is not None


async def test_a_second_unaided_answer_later_marks_retention(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    later = T0 + timedelta(days=get_settings().retention_min_days)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "b"), when=later)

    assert (await _state(db_session, learner, kc)).retention_shown_at is not None


async def test_a_self_rating_moves_no_milestone(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(
        db_session, learner, kc, await _item(db_session, kc, "a"), when=T0, self_rated=True
    )

    state = await _state(db_session, learner, kc)
    assert state.unaided_last_at is None
    assert state.retention_shown_at is None


async def test_retention_checks_read_no_events_once_marked(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)

    with count_queries(db_session) as q:
        due = await mastery.due_retention_checks(
            db_session, learner.id, now=T0 + timedelta(days=365)
        )

    assert [c.kc_id for c in due] == [kc.id]
    assert not [s for s in q.statements if "learning_events" in s], q


async def test_unmarked_states_are_marked_on_first_read(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
    # What every row looks like straight after the migration.
    await db_session.execute(
        update(LearnerKCState)
        .where(LearnerKCState.learner_id == learner.id)
        .values(unaided_last_at=None, retention_shown_at=None, evidence_marked_at=None)
    )
    await db_session.flush()

    due = await mastery.due_retention_checks(db_session, learner.id, now=T0 + timedelta(days=365))

    assert [c.kc_id for c in due] == [kc.id]
    state = await _state(db_session, learner, kc)
    await db_session.refresh(state)
    assert state.evidence_marked_at is not None
    assert state.unaided_last_at is not None
    assert mastery.naive_utc(state.unaided_last_at) == mastery.naive_utc(T0)


async def test_clearing_the_mark_recomputes_it(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    later = T0 + timedelta(days=get_settings().retention_min_days)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "b"), when=later)
    # A stale "shown" (as after raising retention_min_days), cleared the way the RUNBOOK says.
    await db_session.execute(
        update(LearnerKCState)
        .where(LearnerKCState.learner_id == learner.id)
        .values(retention_shown_at=None, evidence_marked_at=None)
    )
    await db_session.flush()

    await mastery.due_retention_checks(db_session, learner.id, now=later + timedelta(days=365))

    state = await _state(db_session, learner, kc)
    await db_session.refresh(state)
    assert state.retention_shown_at is not None


async def test_transfer_reads_evidence_only_for_the_capped_candidates(
    db_session, monkeypatch
) -> None:
    learner = await _learner(db_session)
    kcs = [(await _kc(db_session))[1] for _ in range(3)]
    later = T0 + timedelta(days=get_settings().retention_min_days)
    for kc in kcs:
        await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
        await _answer(db_session, learner, kc, await _item(db_session, kc, "b"), when=later)

    asked: list[set] = []
    real = mastery.kc_evidence

    async def spy(session, learner_id, kc_ids):
        asked.append(set(kc_ids))
        return await real(session, learner_id, kc_ids)

    monkeypatch.setattr(mastery, "kc_evidence", spy)
    await mastery.due_transfer_checks(
        db_session, learner.id, now=later + timedelta(days=365), limit=1
    )

    assert asked and all(len(ids) <= 1 for ids in asked), asked


async def test_due_checks_can_be_scoped_to_components(db_session) -> None:
    learner = await _learner(db_session)
    _s1, kc1 = await _kc(db_session)
    _s2, kc2 = await _kc(db_session)
    for kc in (kc1, kc2):
        await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)

    due = await mastery.due_retention_checks(
        db_session, learner.id, kc_ids={kc1.id}, now=T0 + timedelta(days=365)
    )

    assert [c.kc_id for c in due] == [kc1.id]
