"""What the report says — the numbers a question is switched live on (S82)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import DecisionCall
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services.decision_report import (
    build_report,
    summarise_grade,
    summarise_intent,
    summarise_operations,
)


def _intent(answer: str, confidence: float, baseline: str | None, **kw) -> DecisionCall:
    return DecisionCall(
        request_id=kw.pop("request_id", uuid.uuid4()),
        question="intent",
        mode="shadow",
        provider="typesafe",
        model="jev-1.13.0",
        status=kw.pop("status", "ok"),
        answer=answer,
        probabilities={answer: confidence},
        confidence=confidence,
        baseline_intent=baseline,
        used=False,
        **kw,
    )


def _grade(p: float, baseline: float | None, **kw) -> DecisionCall:
    return DecisionCall(
        request_id=kw.pop("request_id", uuid.uuid4()),
        question="fully_correct",
        mode="shadow",
        provider="typesafe",
        model="jev-1.13.0",
        status="ok",
        probabilities={"yes": p},
        baseline_score=baseline,
        used=False,
        **kw,
    )


def test_intent_agreement_bands_and_harmful_directions() -> None:
    rows = [
        _intent("attempt", 0.95, "attempt"),
        _intent("deferral", 0.92, "deferral"),
        _intent("attempt", 0.93, "deferral"),  # confident: a non-answer would be graded
        _intent("withdrawal", 0.91, "attempt"),  # confident: evidence would be dropped
        _intent("attempt", 0.6, "withdrawal"),  # not confident: would have fallen back
        _intent("attempt", 0.99, None),  # live-used: no baseline, not compared
        _intent("attempt", 0.99, "attempt", status="timeout"),  # failed: not compared
    ]

    s = summarise_intent(rows, threshold=0.9)

    assert (s.compared, s.agreed) == (5, 2)
    assert s.confident == 4 and s.confident_agreed == 2
    assert s.graded_non_answers == 1
    assert s.dropped_attempts == 1
    assert s.confusion[("attempt", "deferral")] == 1  # (jev, baseline)
    top = s.bands[-1]
    assert (top.low, top.rows, top.agreed) == (0.9, 4, 2)


def test_grade_false_passes_are_counted_among_confident_passes_only() -> None:
    rows = [
        _grade(0.97, 1.0),  # confident, SMART agrees fully
        _grade(0.95, 0.8),  # confident, SMART short of full marks
        _grade(0.93, 0.3),  # confident, SMART failed it: a false pass
        _grade(0.5, 0.1),  # not confident: SMART would still have graded it
        _grade(0.99, None),  # live-used: no baseline
    ]

    s = summarise_grade(rows, threshold=0.9)

    assert s.compared == 4
    assert s.confident == 3
    assert s.below_full == 2
    assert s.mean_shortfall == pytest.approx((0.2 + 0.7) / 2)
    assert s.false_passes == 1


def test_a_shared_request_is_counted_once_for_spend_and_latency() -> None:
    shared = uuid.uuid4()
    rows = [
        _intent("attempt", 0.9, "attempt", request_id=shared, cost_usd=0.001, latency_ms=100),
        _grade(0.9, 1.0, request_id=shared, cost_usd=0.001, latency_ms=100),
        _grade(0.9, 1.0, cost_usd=0.002, latency_ms=300),
    ]

    ops = summarise_operations(rows)

    assert ops.requests == 2
    assert ops.spend_usd == pytest.approx(0.003)
    assert ops.statuses == {"ok": 3}
    assert (ops.p50, ops.p99) == (100, 300)


def test_no_rows_is_a_report_not_a_crash() -> None:
    assert summarise_intent([], threshold=0.9).compared == 0
    assert summarise_grade([], threshold=0.9).mean_shortfall is None
    assert summarise_operations([]).p50 is None


def test_a_request_with_a_still_running_row_uses_the_siblings_values() -> None:
    """The first row written (while live was still waiting at the deadline) has no cost or
    latency; a sibling row on the same request, written after it finished, does."""
    shared = uuid.uuid4()
    rows = [
        _intent("attempt", 0.9, "attempt", request_id=shared),  # no cost/latency yet
        _grade(0.9, 1.0, request_id=shared, cost_usd=0.004, latency_ms=250),
    ]

    ops = summarise_operations(rows)

    assert ops.requests == 1
    assert ops.spend_usd == pytest.approx(0.004)
    assert ops.p50 == 250
    assert ops.no_latency == 0


def test_a_request_with_no_latency_anywhere_is_excluded_and_counted() -> None:
    rows = [_intent("attempt", 0.9, "attempt")]  # still running when recorded: no latency at all

    ops = summarise_operations(rows)

    assert ops.no_latency == 1
    assert ops.p50 is None


async def test_the_report_shows_a_false_pass_with_the_learners_answer(
    db_session: AsyncSession,
) -> None:
    learner = Learner(handle=f"dr-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    attempt = uuid.uuid4()
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            event_type="observation",
            attempt_id=attempt,
            payload={"response": {"text": "the mitochondria is the powerhouse"}, "score": 0.2},
        )
    )
    db_session.add(_grade(0.96, 0.2, learner_id=learner.id, attempt_id=attempt))
    await db_session.flush()

    text = await build_report(
        db_session,
        since=datetime(2000, 1, 1, tzinfo=UTC),
        intent_threshold=0.9,
        grade_threshold=0.9,
        examples=5,
    )

    assert "false passes" in text.lower()
    assert "the mitochondria is the powerhouse" in text


async def test_the_report_counts_requests_with_no_latency_recorded(
    db_session: AsyncSession,
) -> None:
    db_session.add(_intent("attempt", 0.9, "attempt"))  # still running when recorded
    await db_session.flush()

    text = await build_report(
        db_session,
        since=datetime(2000, 1, 1, tzinfo=UTC),
        intent_threshold=0.9,
        grade_threshold=0.9,
        examples=0,
    )

    assert "no latency" in text.lower()
    assert "1" in text.split("no latency")[1].splitlines()[0]
