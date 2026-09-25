"""A head start carried over an accepted concept link, and what confirms it (S24)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import get_settings
from app.learning import mastery
from app.learning.mastery import Observation
from app.models.learning import LearnerKCState, LearningEvent
from app.services import lesson_plan as lesson_plan_svc
from tests.test_concept_links import _kc, _learner, _subject


async def _measured(session, learner, kc, ability, uncertainty):
    session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=kc.id,
            ability=ability,
            uncertainty=uncertainty,
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _pair(session):
    learner = await _learner(session)
    source = await _kc(session, await _subject(session, name="Calculus"), "Derivatives", None)
    target = await _kc(session, await _subject(session, name="Physics"), "Derivatives", None)
    return learner, source, target


async def _answer(session, learner, kc, score, item=None):
    await mastery.record_observation(
        session,
        Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=score, item_id=item),
    )


async def _state(session, learner, kc) -> LearnerKCState:
    return await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == kc.id
        )
    )


async def test_a_seed_takes_the_source_estimate_with_widened_uncertainty(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    link = uuid.uuid4()
    state = await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link
    )
    assert state is not None
    assert state.ability == 2.0
    assert state.uncertainty == get_settings().transfer_uncertainty_floor
    assert state.last_seen_at is None and mastery.is_provisional(state)
    event = await db_session.scalar(
        select(LearningEvent).where(
            LearningEvent.kc_id == target.id,
            LearningEvent.event_type == mastery.TRANSFER_SEED_EVENT,
        )
    )
    assert event.payload["source_kc_id"] == str(source.id) and event.payload["link_id"] == str(link)
    # The source is untouched.
    assert (await _state(db_session, learner, source)).uncertainty == 0.3


async def test_real_evidence_on_the_target_is_never_overwritten(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await _measured(db_session, learner, target, -0.5, 0.7)
    assert (
        await mastery.seed_transfer(
            db_session,
            learner.id,
            target_kc_id=target.id,
            source_kc_id=source.id,
            link_id=uuid.uuid4(),
        )
        is None
    )
    assert (await _state(db_session, learner, target)).ability == -0.5


async def test_an_unmeasured_source_seeds_nothing(db_session):
    learner, source, target = await _pair(db_session)
    assert (
        await mastery.seed_transfer(
            db_session,
            learner.id,
            target_kc_id=target.id,
            source_kc_id=source.id,
            link_id=uuid.uuid4(),
        )
        is None
    )


async def test_the_strongest_source_wins(db_session):
    learner, weak, target = await _pair(db_session)
    strong = await _kc(
        db_session, await _subject(db_session, name="Mechanics"), "Derivatives", None
    )
    await _measured(db_session, learner, weak, 1.0, 0.3)
    await _measured(db_session, learner, strong, 2.5, 0.3)
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=strong.id, link_id=uuid.uuid4()
    )
    assert (
        await mastery.seed_transfer(
            db_session,
            learner.id,
            target_kc_id=target.id,
            source_kc_id=weak.id,
            link_id=uuid.uuid4(),
        )
        is None
    )
    assert (await _state(db_session, learner, target)).transferred_from_kc_id == strong.id


async def test_a_wrong_first_answer_is_never_mastery(db_session):
    """Review focus 2: 2.0/0.6 → 1.69/0.59 after a miss still clears the bar numerically."""
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4()
    )
    await _answer(db_session, learner, target, 0.0)
    assert target.id not in await lesson_plan_svc.mastered_kc_ids(
        db_session, learner.id, [target.id]
    )
    assert (await _state(db_session, learner, target)).achieved_at is None


async def test_one_pass_is_not_enough_and_two_on_different_questions_confirm(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4()
    )
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    assert mastery.is_provisional(await _state(db_session, learner, target))
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    state = await _state(db_session, learner, target)
    assert not mastery.is_provisional(state) and state.transfer_confirmed_at is not None
    assert target.id in await lesson_plan_svc.mastered_kc_ids(db_session, learner.id, [target.id])


async def test_the_same_question_twice_confirms_nothing(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4()
    )
    item = uuid.uuid4()
    await _answer(db_session, learner, target, 1.0, item=item)
    await _answer(db_session, learner, target, 1.0, item=item)
    assert mastery.is_provisional(await _state(db_session, learner, target))


async def test_a_failure_restarts_the_run(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4()
    )
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    await _answer(db_session, learner, target, 0.0, item=uuid.uuid4())
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    assert mastery.is_provisional(await _state(db_session, learner, target))


async def test_revoking_before_any_answer_removes_the_head_start(db_session):
    """Review focus 3."""
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    link = uuid.uuid4()
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link
    )
    assert await mastery.revoke_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link
    )
    state = await _state(db_session, learner, target)
    assert (state.ability, state.uncertainty, state.transferred_at) == (0.0, 1.0, None)
    assert target.id not in await mastery.provisional_kc_ids(db_session, learner.id, [target.id])


async def test_revoking_after_answers_keeps_the_estimate(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    link = uuid.uuid4()
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link
    )
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    before = (await _state(db_session, learner, target)).ability
    assert not await mastery.revoke_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link
    )
    state = await _state(db_session, learner, target)
    assert state.ability == before and mastery.is_provisional(state)


async def test_a_seed_is_not_evidence(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(
        db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4()
    )
    assert await mastery.kc_evidence(db_session, learner.id, [target.id]) == {}
