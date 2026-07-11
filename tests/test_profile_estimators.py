"""Learner-profile dimension estimators.

Per-dimension estimator tests are added alongside each estimator (see the commit sequence in
the learner-profile plan) — this file grows as `DIMENSION_SPECS` grows.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.profile_estimators import (
    EstimatorContext,
    _cluster_sessions,
    _estimate_cognitive_load_tolerance,
    _estimate_optimal_challenge,
    _estimate_pace,
)
from app.learning.profile_estimators import _estimate_error_type as estimate_error_type
from app.llm import LLMClient
from app.llm.registry import fake_llm_client
from app.models.assessment import Item, ItemType
from app.models.learning import LearningEvent


def _event(minutes_offset: int) -> LearningEvent:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return LearningEvent(
        id=uuid.uuid4(),
        learner_id=uuid.uuid4(),
        event_type="observation",
        payload={},
        created_at=base + timedelta(minutes=minutes_offset),
    )


def _obs(
    *,
    score: float = 1.0,
    difficulty: float = 0.0,
    latency_ms: int | None = None,
    item_id: uuid.UUID | None = None,
    response: dict | None = None,
    minutes_offset: int = 0,
) -> LearningEvent:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return LearningEvent(
        id=uuid.uuid4(),
        learner_id=uuid.uuid4(),
        event_type="observation",
        payload={
            "score": score,
            "difficulty": difficulty,
            "latency_ms": latency_ms,
            "item_id": str(item_id) if item_id else None,
            "response": response,
        },
        created_at=base + timedelta(minutes=minutes_offset),
    )


def _ctx(
    session: AsyncSession, events: list[LearningEvent], *, llm: LLMClient | None = None
) -> EstimatorContext:
    return EstimatorContext(
        session=session,
        learner_id=uuid.uuid4(),
        events=events,
        messages=[],
        llm=llm or fake_llm_client(),
    )


def test_cluster_sessions_splits_on_gap() -> None:
    events = [_event(0), _event(5), _event(10), _event(100), _event(105)]
    sessions = _cluster_sessions(events, gap_minutes=30)
    assert [len(s) for s in sessions] == [3, 2]


def test_cluster_sessions_empty_input() -> None:
    assert _cluster_sessions([], gap_minutes=30) == []


def test_cluster_sessions_single_session_when_no_gap_exceeded() -> None:
    events = [_event(0), _event(10), _event(20)]
    sessions = _cluster_sessions(events, gap_minutes=30)
    assert len(sessions) == 1
    assert len(sessions[0]) == 3


# --- pace -------------------------------------------------------------


async def test_estimate_pace_below_threshold_returns_none(db_session: AsyncSession) -> None:
    ctx = _ctx(db_session, [_obs(latency_ms=1000), _obs(latency_ms=2000)])
    estimate, usage = await _estimate_pace(ctx)
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_pace_detects_speeding_up_trend(db_session: AsyncSession) -> None:
    events = [
        _obs(latency_ms=10_000, minutes_offset=0),
        _obs(latency_ms=10_000, minutes_offset=1),
        _obs(latency_ms=2_000, minutes_offset=2),
        _obs(latency_ms=2_000, minutes_offset=3),
    ]
    estimate, _ = await _estimate_pace(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value["trend"] == "speeding_up"
    assert estimate.value["median_seconds"] == pytest.approx(6.0)


async def test_estimate_pace_ignores_events_missing_latency(db_session: AsyncSession) -> None:
    events = [_obs(latency_ms=None), _obs(latency_ms=None), _obs(latency_ms=1000)]
    estimate, _ = await _estimate_pace(_ctx(db_session, events))
    assert estimate is None  # only 1 usable event, below threshold


# --- optimal_challenge -------------------------------------------------


async def test_estimate_optimal_challenge_uses_productive_struggle_band(
    db_session: AsyncSession,
) -> None:
    events = [
        _obs(score=0.6, difficulty=0.5),
        _obs(score=0.5, difficulty=0.7),
        _obs(score=0.7, difficulty=0.6),
        _obs(score=1.0, difficulty=0.9),  # outside the band — excluded
    ]
    estimate, _ = await _estimate_optimal_challenge(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value == pytest.approx(0.6, abs=0.01)


async def test_estimate_optimal_challenge_falls_back_when_band_thin(
    db_session: AsyncSession,
) -> None:
    events = [_obs(score=1.0, difficulty=0.9) for _ in range(3)]
    estimate, _ = await _estimate_optimal_challenge(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value == pytest.approx(0.9)


async def test_estimate_optimal_challenge_below_threshold_returns_none(
    db_session: AsyncSession,
) -> None:
    estimate, _ = await _estimate_optimal_challenge(_ctx(db_session, [_obs(), _obs()]))
    assert estimate is None


# --- cognitive_load_tolerance -------------------------------------------


async def test_estimate_cognitive_load_tolerance_needs_a_full_session(
    db_session: AsyncSession,
) -> None:
    events = [_obs(score=1.0, minutes_offset=i) for i in range(3)]  # short of the 4-event floor
    estimate, _ = await _estimate_cognitive_load_tolerance(_ctx(db_session, events))
    assert estimate is None


async def test_estimate_cognitive_load_tolerance_detects_accuracy_drop(
    db_session: AsyncSession,
) -> None:
    events = [
        _obs(score=1.0, minutes_offset=0),
        _obs(score=1.0, minutes_offset=1),
        _obs(score=0.0, minutes_offset=2),
        _obs(score=0.0, minutes_offset=3),
    ]
    estimate, _ = await _estimate_cognitive_load_tolerance(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value < 0


# --- error_type ----------------------------------------------------------


async def test_estimate_error_type_below_threshold_returns_none(db_session: AsyncSession) -> None:
    events = [_obs(score=0.0, item_id=uuid.uuid4())]
    estimate, usage = await estimate_error_type(_ctx(db_session, events))
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_error_type_classifies_via_one_batched_call(
    db_session: AsyncSession,
) -> None:
    events = []
    for i in range(3):
        item = Item(
            item_type=ItemType.MCQ,
            stem=f"Q{i}",
            answer_key={"choices": ["A", "B", "C"], "correct": 0},
            difficulty=0.5,
        )
        db_session.add(item)
        await db_session.flush()
        events.append(_obs(score=0.0, item_id=item.id, response={"choice": 1}, minutes_offset=i))
    reply = json.dumps(
        {
            "classifications": [
                {"item": 1, "type": "conceptual"},
                {"item": 2, "type": "procedural"},
                {"item": 3, "type": "conceptual"},
            ]
        }
    )
    ctx = _ctx(db_session, events, llm=fake_llm_client(reply))
    estimate, usage = await estimate_error_type(ctx)
    assert estimate is not None
    assert estimate.value["conceptual"] == pytest.approx(2 / 3, abs=0.01)
    assert usage.output_tokens > 0


async def test_estimate_error_type_tolerates_unparseable_reply(
    db_session: AsyncSession,
) -> None:
    events = []
    for i in range(3):
        item = Item(
            item_type=ItemType.MCQ, stem=f"Q{i}", answer_key={"choices": ["A"], "correct": 0}
        )
        db_session.add(item)
        await db_session.flush()
        events.append(_obs(score=0.0, item_id=item.id, minutes_offset=i))
    ctx = _ctx(db_session, events, llm=fake_llm_client("not json"))
    estimate, usage = await estimate_error_type(ctx)
    assert estimate is None
    assert usage.total_tokens > 0  # the call happened; it just didn't parse
