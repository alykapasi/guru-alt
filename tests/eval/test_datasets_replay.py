"""Replaying a mined history has to reproduce the state production stored (S56).

These build history through ``record_observation`` itself rather than by hand-writing events,
so the thing under test is the real production update path: placement priors, time decay,
multi-KC apportioning and assistance discounting included.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import mastery
from app.learning.mastery import Observation
from app.learning.tracer import Estimate, GlickoEstimator
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from tests.eval.datasets.calibration import score_tracer_calibration
from tests.eval.datasets.mine import mine_observation_sequences
from tests.eval.datasets.replay import replay_sequence, verify_replay

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


async def _learner_and_kcs(session: AsyncSession, n: int = 2) -> tuple[Learner, list[KC]]:
    learner = Learner(handle=f"replay-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
    session.add(topic)
    await session.flush()
    kcs = [
        KC(topic_id=topic.id, slug=f"k{i}-{uuid.uuid4().hex[:6]}", name=f"k{i}") for i in range(n)
    ]
    session.add_all(kcs)
    await session.flush()
    return learner, kcs


async def _history(
    session: AsyncSession, learner: Learner, kcs: list[KC], *, seed: Estimate | None = None
) -> None:
    """A history with everything the old replay dropped: a seed, real gaps, split weights, hints."""
    if seed is not None:
        await mastery.seed_prior(session, learner.id, kcs[0].id, seed)
    plan = [
        # (days after T0, score, difficulty, weights, hints, prior attempts)
        (0.0, 1.0, 0.0, {kcs[0].id: 1.0}, None, 0),
        (3.0, 0.5, 0.5, {kcs[0].id: 2.0, kcs[1].id: 1.0}, None, 0),
        (17.0, 0.0, -0.5, {kcs[0].id: 1.0}, 2, 1),
        (17.5, 1.0, 1.0, {kcs[0].id: 1.0, kcs[1].id: 3.0}, None, 0),
        (60.0, 0.75, 0.25, {kcs[0].id: 1.0}, None, 0),
    ]
    for days, score, difficulty, weights, hints, priors in plan:
        await mastery.record_observation(
            session,
            Observation(
                learner_id=learner.id,
                kc_weights=weights,
                score=score,
                difficulty=difficulty,
                hints_used=hints,
                prior_attempts=priors,
            ),
            now=T0 + timedelta(days=days),
        )
    await session.flush()


async def test_replay_reproduces_the_stored_state(db_session: AsyncSession) -> None:
    learner, kcs = await _learner_and_kcs(db_session)
    await _history(db_session, learner, kcs)

    dataset = await mine_observation_sequences(db_session, min_length=3)
    report = verify_replay(dataset)

    assert report.sequences_total >= 1
    assert report.sequences_replayable == report.sequences_total
    assert report.clean, report.failures
    assert report.max_ability_error is not None and report.max_ability_error < 1e-9


async def test_replay_starts_from_the_placement_prior(db_session: AsyncSession) -> None:
    learner, kcs = await _learner_and_kcs(db_session)
    await _history(db_session, learner, kcs, seed=Estimate(ability=1.4, uncertainty=0.6))

    dataset = await mine_observation_sequences(db_session, min_length=3)
    seeded = next(s for s in dataset.sequences if s.kc_id == str(kcs[0].id))

    assert seeded.seed is not None
    assert seeded.seed.ability == pytest.approx(1.4)
    assert verify_replay(dataset).clean

    # And the seed is load-bearing: dropping it changes where the replay lands, which is
    # precisely the error the old replay made silently on every placed learner.
    from_default = replay_sequence(seeded.model_copy(update={"seed": None}))[-1]
    assert from_default.ability != pytest.approx(replay_sequence(seeded)[-1].ability)


async def test_replay_matches_the_state_row_itself(db_session: AsyncSession) -> None:
    learner, kcs = await _learner_and_kcs(db_session)
    await _history(db_session, learner, kcs, seed=Estimate(ability=-0.8, uncertainty=0.9))

    dataset = await mine_observation_sequences(db_session, min_length=3)
    for seq in dataset.sequences:
        # The stored row, not estimate_kc() — that decays to *now*, and what a replay has to
        # reproduce is what production wrote at the time of the last observation.
        state = await db_session.scalar(
            select(LearnerKCState).where(
                LearnerKCState.learner_id == learner.id,
                LearnerKCState.kc_id == uuid.UUID(seq.kc_id),
            )
        )
        assert state is not None
        final = replay_sequence(seq)[-1]
        assert final.ability == pytest.approx(state.ability, abs=1e-9)
        assert final.uncertainty == pytest.approx(state.uncertainty, abs=1e-9)


async def test_a_sequence_recorded_before_the_replay_fields_says_so(
    db_session: AsyncSession,
) -> None:
    learner, kcs = await _learner_and_kcs(db_session)
    await _history(db_session, learner, kcs)
    dataset = await mine_observation_sequences(db_session, min_length=3)

    # A version-1 row: score and difficulty only, as the log held before S56.
    old = dataset.sequences[0].model_copy(
        update={
            "steps": [s.model_copy(update={"weight": None}) for s in dataset.sequences[0].steps]
        }
    )
    assert not old.replayable
    result = verify_replay(dataset.model_copy(update={"sequences": [old]}))
    assert result.sequences_replayable == 0
    assert result.failures[0].reason is not None
    # It is reported, not silently replayed from defaults and counted as agreement.
    assert not result.failures[0].faithful


async def test_an_unknown_estimator_refuses_rather_than_substituting(
    db_session: AsyncSession,
) -> None:
    learner, kcs = await _learner_and_kcs(db_session)
    await _history(db_session, learner, kcs)
    dataset = await mine_observation_sequences(db_session, min_length=3)
    renamed = dataset.sequences[0].model_copy(update={"estimator": "dkt-v1"})

    with pytest.raises(ValueError, match="dkt-v1"):
        replay_sequence(renamed)


async def test_calibration_scores_the_prediction_production_actually_made(
    db_session: AsyncSession,
) -> None:
    learner, kcs = await _learner_and_kcs(db_session)
    await _history(db_session, learner, kcs, seed=Estimate(ability=1.2, uncertainty=0.7))
    dataset = await mine_observation_sequences(db_session, min_length=3)

    shipped = score_tracer_calibration(dataset)
    assert shipped.n_recorded_predictions == shipped.n_points

    # Passing a candidate estimator means its own predictions, not the recorded ones.
    candidate = score_tracer_calibration(dataset, estimator=GlickoEstimator(volatility=0.5))
    assert candidate.n_recorded_predictions == 0
    assert candidate.mae != pytest.approx(shipped.mae)


async def test_a_seed_cannot_land_after_evidence(db_session: AsyncSession) -> None:
    """What makes a mined seed safe to treat as the sequence's starting state.

    The miner used to guard this by comparing timestamps, which cannot work: `created_at` is
    the transaction's clock, so a seed and the observations committed with it tie. The
    invariant belongs where it is actually enforced.
    """
    learner, kcs = await _learner_and_kcs(db_session, n=1)
    await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={kcs[0].id: 1.0}, score=1.0),
        now=T0,
    )
    refused = await mastery.seed_prior(
        db_session, learner.id, kcs[0].id, Estimate(ability=2.0, uncertainty=0.3)
    )
    assert refused is None


async def test_steps_are_ordered_by_when_the_update_ran(db_session: AsyncSession) -> None:
    """Every event in one transaction shares a `created_at`, so row order decided the sequence.

    Recording observations out of chronological order — a backfill, or simply two answers in
    the same commit — used to produce a sequence in the wrong order, and therefore a replay of
    a history that never happened.
    """
    learner, kcs = await _learner_and_kcs(db_session, n=1)
    for days, score in ((5.0, 0.2), (1.0, 0.9), (9.0, 0.4)):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kcs[0].id: 1.0}, score=score),
            now=T0 + timedelta(days=days),
        )
    dataset = await mine_observation_sequences(db_session, min_length=3)
    (seq,) = dataset.sequences

    assert [s.score for s in seq.steps] == [0.9, 0.2, 0.4]
    # Production applied these in the order they were *called*, and its decay clock only ever
    # moves forward, so chronological order is not what it did. The report says the replay
    # does not reproduce the stored state, which is the whole point — a history recorded out
    # of order is a fact about the log, not something the replay should paper over.
    assert not verify_replay(dataset).clean
