"""A check is arranged for the second unaided demonstration retention needs (S14)."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import lesson_plan as engine
from app.learning import mastery
from app.learning.mastery import Observation
from app.models.assessment import ItemType
from app.models.knowledge import KC
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.services import lesson_plan as plan_svc
from app.services import session_runner
from app.services.lesson_plan import PlanGroundingContext
from tests.test_item_exposure import T0, _item, _kc, _learner


async def _answer(session, learner, kc, item, *, when, hints=None, taught_first=False) -> None:
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            item_id=item.id,
            hints_used=hints,
            taught_first=taught_first,
        ),
        now=when,
    )
    await session.flush()


async def _fsrs_due(session: AsyncSession, learner: Learner, kc: KC, when: datetime) -> None:
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == kc.id
        )
    )
    assert state is not None
    state.due_at = when
    await session.flush()


async def _due(session: AsyncSession, learner: Learner, now: datetime) -> list[uuid.UUID]:
    return [c.kc_id for c in await mastery.due_retention_checks(session, learner.id, now=now)]


async def _one_answer(session: AsyncSession) -> tuple[Learner, KC]:
    learner = await _learner(session)
    _subject, kc = await _kc(session)
    await _answer(session, learner, kc, await _item(session, kc, "a"), when=T0)
    await _fsrs_due(session, learner, kc, T0 + timedelta(days=60))  # FSRS far away by default
    return learner, kc


async def test_an_answer_after_a_worked_example_is_not_unaided(db_session) -> None:
    """Review focus 1."""
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    item = await _item(db_session, kc, "a")
    await _answer(db_session, learner, kc, item, when=T0, taught_first=True)
    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()
    assert ev.unassisted_attempts == 0 and ev.last_unassisted_at is None
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_nothing_is_due_before_retention_min_days(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    await _fsrs_due(db_session, learner, kc, T0 + timedelta(hours=6))
    assert await _due(db_session, learner, T0 + timedelta(hours=12)) == []


async def test_fsrs_brings_a_check_forward_once_it_could_count(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    await _fsrs_due(db_session, learner, kc, T0 + timedelta(days=2))
    assert await _due(db_session, learner, T0 + timedelta(days=3)) == [kc.id]


async def test_the_interval_brings_a_check_without_fsrs(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    assert await _due(db_session, learner, T0 + timedelta(days=6)) == []
    [check] = await mastery.due_retention_checks(
        db_session, learner.id, now=T0 + timedelta(days=7, hours=1)
    )
    assert check.kc_id == kc.id
    assert check.due_at.replace(tzinfo=None) == (T0 + timedelta(days=7)).replace(tzinfo=None)


async def test_no_check_without_an_unaided_answer(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0, hints=2)
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_no_check_once_retention_is_shown(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    item = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, item, when=T0 + timedelta(days=8))
    assert await _due(db_session, learner, T0 + timedelta(days=40)) == []


async def test_a_probe_interval_below_the_minimum_is_raised_to_it(db_session, monkeypatch) -> None:
    """Review focus 4."""
    monkeypatch.setattr(get_settings(), "retention_probe_days", 0.25)
    monkeypatch.setattr(get_settings(), "retention_min_days", 2.0)
    learner, _kc_row = await _one_answer(db_session)
    assert await _due(db_session, learner, T0 + timedelta(days=1)) == []
    assert len(await _due(db_session, learner, T0 + timedelta(days=2, hours=1))) == 1


# --- a due check becomes a cold review step ----------------------------------------------------


def _open_reviews(steps: list[engine.StepDict]) -> list[engine.StepDict]:
    return [
        s for s in steps if s["step_type"] == "review" and s["status"] not in ("done", "skipped")
    ]


def test_a_due_check_is_a_flagged_review_step() -> None:
    kc = uuid.uuid4()
    steps = engine.revise_steps(
        [],
        mastered_kc_ids=[],
        due_review_kc_ids=[kc],
        retention_check_kc_ids=[kc],
        scaffolding=engine.ScaffoldingHints(),
    )
    [step] = _open_reviews(steps)
    assert step["retention_check"] is True


def test_an_ordinary_review_is_not_flagged() -> None:
    kc = uuid.uuid4()
    steps = engine.revise_steps(
        [], mastered_kc_ids=[], due_review_kc_ids=[kc], scaffolding=engine.ScaffoldingHints()
    )
    [step] = _open_reviews(steps)
    assert step.get("retention_check") is False


async def test_the_plan_merges_checks_with_fsrs_reviews_once(db_session) -> None:
    """Review focus 3: due both ways is one entry, flagged."""
    learner, kc = await _one_answer(db_session)
    await _fsrs_due(db_session, learner, kc, T0 + timedelta(days=2))
    due = await plan_svc._due_review_kc_ids(
        db_session, learner.id, {kc.id}, now=T0 + timedelta(days=8)
    )
    assert due.kc_ids == [kc.id] and due.retention_checks == frozenset({kc.id})


async def test_an_ordinary_due_review_is_not_a_check(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    item = await _item(db_session, kc, "a")
    await _answer(db_session, learner, kc, item, when=T0, hints=1)
    await _fsrs_due(db_session, learner, kc, T0 + timedelta(days=1))
    due = await plan_svc._due_review_kc_ids(
        db_session, learner.id, {kc.id}, now=T0 + timedelta(days=2)
    )
    assert due.kc_ids == [kc.id] and due.retention_checks == frozenset()


async def test_an_unaided_answer_closes_the_check(db_session) -> None:
    """Review focus 3: answered unaided, the clock restarts — not due at day 7."""
    learner, kc = await _one_answer(db_session)
    item = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, item, when=T0 + timedelta(hours=2))
    assert await _due(db_session, learner, T0 + timedelta(days=7, hours=1)) == []


async def test_a_check_answered_after_a_hint_stays_due(db_session) -> None:
    """Review focus 2."""
    learner, kc = await _one_answer(db_session)
    item = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, item, when=T0 + timedelta(days=7, hours=2), hints=1)
    assert await _due(db_session, learner, T0 + timedelta(days=7, hours=3)) == [kc.id]


def _review_context(*, retention_check: bool) -> PlanGroundingContext:
    return PlanGroundingContext(
        subject_name="S",
        kc_id=uuid.uuid4(),
        kc_name="K",
        step_type="review",
        target_difficulty=None,
        hint_density=None,
        preferred_item_type="flashcard",
        retention_check=retention_check,
    )


def test_a_retention_check_asks_a_written_question_on_the_session_surface() -> None:
    assert (
        session_runner._effective_item_type(_review_context(retention_check=True)) == ItemType.SHORT
    )
    assert (
        session_runner._effective_item_type(_review_context(retention_check=False))
        == ItemType.FLASHCARD
    )
