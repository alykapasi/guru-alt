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
    _estimate_engagement,
    _estimate_error_type,
    _estimate_goal_orientation,
    _estimate_help_seeking,
    _estimate_interests,
    _estimate_message_writing_complexity,
    _estimate_optimal_challenge,
    _estimate_pace,
    _estimate_persistence,
    _estimate_score_by_format,
    _estimate_session_logistics,
    _estimate_within_session_accuracy_drift,
    _observations,
)
from app.llm import LLMClient
from app.llm.registry import fake_llm_client
from app.models.assessment import Item, ItemType
from app.models.chat import Conversation, Message
from app.models.learner import Learner
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
    hints_used: int | None = None,
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
            "hints_used": hints_used,
            "item_id": str(item_id) if item_id else None,
            "response": response,
        },
        created_at=base + timedelta(minutes=minutes_offset),
    )


def _msg(content: str) -> Message:
    return Message(id=uuid.uuid4(), conversation_id=uuid.uuid4(), role="user", content=content)


def _ctx(
    session: AsyncSession,
    events: list[LearningEvent],
    *,
    llm: LLMClient | None = None,
    learner_id: uuid.UUID | None = None,
    messages: list[Message] | None = None,
) -> EstimatorContext:
    return EstimatorContext(
        session=session,
        learner_id=learner_id or uuid.uuid4(),
        events=events,
        messages=messages or [],
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


async def test_estimate_within_session_accuracy_drift_needs_a_full_session(
    db_session: AsyncSession,
) -> None:
    events = [_obs(score=1.0, minutes_offset=i) for i in range(3)]  # short of the 4-event floor
    estimate, _ = await _estimate_within_session_accuracy_drift(_ctx(db_session, events))
    assert estimate is None


async def test_estimate_within_session_accuracy_drift_detects_accuracy_drop(
    db_session: AsyncSession,
) -> None:
    events = [
        _obs(score=1.0, minutes_offset=0),
        _obs(score=1.0, minutes_offset=1),
        _obs(score=0.0, minutes_offset=2),
        _obs(score=0.0, minutes_offset=3),
    ]
    estimate, _ = await _estimate_within_session_accuracy_drift(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value < 0


# --- error_type ----------------------------------------------------------


async def test_estimate_error_type_below_threshold_returns_none(db_session: AsyncSession) -> None:
    events = [_obs(score=0.0, item_id=uuid.uuid4())]
    estimate, usage = await _estimate_error_type(_ctx(db_session, events))
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
    estimate, usage = await _estimate_error_type(ctx)
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
    estimate, usage = await _estimate_error_type(ctx)
    assert estimate is None
    assert usage.total_tokens > 0  # the call happened; it just didn't parse


# --- help_seeking ----------------------------------------------------------


async def test_estimate_help_seeking_below_threshold_returns_none(db_session: AsyncSession) -> None:
    events = [_obs(hints_used=1), _obs(hints_used=2)]
    estimate, usage = await _estimate_help_seeking(_ctx(db_session, events))
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_help_seeking_computes_mean(db_session: AsyncSession) -> None:
    events = [_obs(hints_used=0), _obs(hints_used=2), _obs(hints_used=4)]
    estimate, _ = await _estimate_help_seeking(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value == pytest.approx(2.0)


async def test_estimate_help_seeking_ignores_events_without_hints(
    db_session: AsyncSession,
) -> None:
    events = [_obs(hints_used=None), _obs(hints_used=None), _obs(hints_used=1)]
    estimate, _ = await _estimate_help_seeking(_ctx(db_session, events))
    assert estimate is None


# --- persistence -------------------------------------------------------


async def test_estimate_persistence_below_threshold_returns_none(db_session: AsyncSession) -> None:
    item_id = uuid.uuid4()
    events = [
        _obs(score=0.0, item_id=item_id, minutes_offset=0),
        _obs(score=1.0, item_id=item_id, minutes_offset=1),
    ]
    estimate, usage = await _estimate_persistence(_ctx(db_session, events))
    assert estimate is None  # only one qualifying item, need >= 2
    assert usage.total_tokens == 0


async def test_estimate_persistence_computes_bounce_back_rate(db_session: AsyncSession) -> None:
    item_a, item_b = uuid.uuid4(), uuid.uuid4()
    events = [
        _obs(score=0.0, item_id=item_a, minutes_offset=0),
        _obs(score=1.0, item_id=item_a, minutes_offset=1),  # bounced back
        _obs(score=0.0, item_id=item_b, minutes_offset=2),
        _obs(score=0.0, item_id=item_b, minutes_offset=3),  # gave up
    ]
    estimate, _ = await _estimate_persistence(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value == pytest.approx(0.5)


async def test_estimate_persistence_ignores_single_attempt_items(
    db_session: AsyncSession,
) -> None:
    events = [_obs(score=0.0, item_id=uuid.uuid4()) for _ in range(5)]
    estimate, _ = await _estimate_persistence(_ctx(db_session, events))
    assert estimate is None


async def test_estimate_persistence_collapses_multi_kc_fanout_into_one_attempt(
    db_session: AsyncSession,
) -> None:
    """A multi-KC item fans one graded answer out into several same-timestamp
    LearningEvent rows (mastery.record_observation) — that must count as one attempt,
    not several, or a single wrong answer would masquerade as a qualifying retry."""
    fanout_item, item_b, item_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        # Two KCs tagged on the same answer: identical score + timestamp, not a retry.
        _obs(score=0.0, item_id=fanout_item, minutes_offset=0),
        _obs(score=0.0, item_id=fanout_item, minutes_offset=0),
        _obs(score=0.0, item_id=item_b, minutes_offset=1),
        _obs(score=1.0, item_id=item_b, minutes_offset=2),  # bounced back
        _obs(score=0.0, item_id=item_c, minutes_offset=3),
        _obs(score=0.0, item_id=item_c, minutes_offset=4),  # gave up
    ]
    estimate, _ = await _estimate_persistence(_ctx(db_session, events))
    assert estimate is not None
    # If the fan-out duplicate were (mis)counted as a qualifying single-attempt-that-gave-up
    # item, this would be 1/3 instead.
    assert estimate.value == pytest.approx(0.5)


# --- engagement ----------------------------------------------------------


async def test_estimate_engagement_no_events_returns_none(db_session: AsyncSession) -> None:
    estimate, usage = await _estimate_engagement(_ctx(db_session, []))
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_engagement_penalizes_error_streaks(db_session: AsyncSession) -> None:
    events = [
        _obs(score=1.0, minutes_offset=0),
        _obs(score=0.0, minutes_offset=1),
        _obs(score=0.0, minutes_offset=2),
        _obs(score=0.0, minutes_offset=3),
    ]
    estimate, _ = await _estimate_engagement(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value == pytest.approx(0.25)  # 1 - (streak of 3 / 4 events)


async def test_estimate_engagement_only_considers_most_recent_session(
    db_session: AsyncSession,
) -> None:
    events = [
        _obs(score=0.0, minutes_offset=0),
        _obs(score=0.0, minutes_offset=1),
        _obs(score=0.0, minutes_offset=2),
        _obs(score=1.0, minutes_offset=1000),
        _obs(score=1.0, minutes_offset=1001),
        _obs(score=1.0, minutes_offset=1002),
    ]
    estimate, _ = await _estimate_engagement(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value == pytest.approx(1.0)


# --- goal_orientation ------------------------------------------------------


async def test_estimate_goal_orientation_no_goals_returns_none(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    ctx = _ctx(db_session, [], learner_id=learner.id)
    estimate, usage = await _estimate_goal_orientation(ctx)
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_goal_orientation_classifies_via_llm(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    db_session.add(Conversation(learner_id=learner.id, goal="I want to truly understand calculus"))
    await db_session.flush()
    reply = json.dumps({"orientation": "mastery", "confidence": 0.8})
    ctx = _ctx(db_session, [], llm=fake_llm_client(reply), learner_id=learner.id)
    estimate, usage = await _estimate_goal_orientation(ctx)
    assert estimate is not None
    assert estimate.value == {"orientation": "mastery", "confidence": 0.8}
    assert usage.output_tokens > 0


async def test_estimate_goal_orientation_tolerates_unparseable_reply(
    db_session: AsyncSession,
) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    db_session.add(Conversation(learner_id=learner.id, goal="ace the exam"))
    await db_session.flush()
    ctx = _ctx(db_session, [], llm=fake_llm_client("garbage"), learner_id=learner.id)
    estimate, usage = await _estimate_goal_orientation(ctx)
    assert estimate is None
    assert usage.total_tokens > 0


# --- interests ------------------------------------------------------------


async def test_estimate_interests_no_messages_returns_none(db_session: AsyncSession) -> None:
    estimate, usage = await _estimate_interests(_ctx(db_session, [], messages=[]))
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_interests_extracts_and_dedupes_via_llm(db_session: AsyncSession) -> None:
    messages = [_msg("I love playing basketball on weekends")]
    reply = json.dumps({"interests": ["basketball", "basketball"]})
    ctx = _ctx(db_session, [], messages=messages, llm=fake_llm_client(reply))
    estimate, usage = await _estimate_interests(ctx)
    assert estimate is not None
    assert estimate.value == ["basketball"]
    assert usage.output_tokens > 0


async def test_estimate_interests_empty_list_returns_none(db_session: AsyncSession) -> None:
    messages = [_msg("hello")]
    reply = json.dumps({"interests": []})
    ctx = _ctx(db_session, [], messages=messages, llm=fake_llm_client(reply))
    estimate, usage = await _estimate_interests(ctx)
    assert estimate is None
    assert usage.total_tokens > 0


# --- reading_level -------------------------------------------------------


async def test_estimate_message_writing_complexity_below_word_threshold_returns_none(
    db_session: AsyncSession,
) -> None:
    messages = [_msg("too short")]
    estimate, usage = await _estimate_message_writing_complexity(
        _ctx(db_session, [], messages=messages)
    )
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_message_writing_complexity_simpler_text_scores_lower(
    db_session: AsyncSession,
) -> None:
    simple = _msg("The cat sat on the mat. " * 10)
    complex_ = _msg(
        "The multifaceted epistemological ramifications necessitate comprehensive "
        "interdisciplinary consideration. " * 10
    )
    simple_estimate, _ = await _estimate_message_writing_complexity(
        _ctx(db_session, [], messages=[simple])
    )
    complex_estimate, _ = await _estimate_message_writing_complexity(
        _ctx(db_session, [], messages=[complex_])
    )
    assert simple_estimate is not None
    assert complex_estimate is not None
    assert simple_estimate.value < complex_estimate.value


# --- session_logistics ----------------------------------------------------


async def test_estimate_session_logistics_below_threshold_returns_none(
    db_session: AsyncSession,
) -> None:
    events = [_obs(minutes_offset=0), _obs(minutes_offset=5)]  # only one session
    estimate, usage = await _estimate_session_logistics(_ctx(db_session, events))
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_session_logistics_computes_typical_length(
    db_session: AsyncSession,
) -> None:
    events = [
        _obs(minutes_offset=0),
        _obs(minutes_offset=10),  # session 1: 10 minutes
        _obs(minutes_offset=1000),
        _obs(minutes_offset=1020),  # session 2: 20 minutes
    ]
    estimate, _ = await _estimate_session_logistics(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value["typical_session_minutes"] == pytest.approx(15.0)


# --- format_effectiveness --------------------------------------------------


async def test_estimate_score_by_format_needs_at_least_two_formats(
    db_session: AsyncSession,
) -> None:
    item = Item(item_type=ItemType.MCQ, stem="Q", answer_key={"choices": ["A"], "correct": 0})
    db_session.add(item)
    await db_session.flush()
    events = [_obs(score=1.0, item_id=item.id, minutes_offset=i) for i in range(5)]
    estimate, usage = await _estimate_score_by_format(_ctx(db_session, events))
    assert estimate is None
    assert usage.total_tokens == 0


async def test_estimate_score_by_format_compares_formats(db_session: AsyncSession) -> None:
    mcq = Item(item_type=ItemType.MCQ, stem="Q1", answer_key={"choices": ["A"], "correct": 0})
    cloze = Item(item_type=ItemType.CLOZE, stem="Q2", answer_key={"blanks": ["x"]})
    db_session.add_all([mcq, cloze])
    await db_session.flush()
    events = [_obs(score=1.0, item_id=mcq.id, minutes_offset=i) for i in range(3)] + [
        _obs(score=0.0, item_id=cloze.id, minutes_offset=10 + i) for i in range(3)
    ]
    estimate, _ = await _estimate_score_by_format(_ctx(db_session, events))
    assert estimate is not None
    assert estimate.value["mcq"]["mean_score"] == pytest.approx(1.0)
    assert estimate.value["cloze"]["mean_score"] == pytest.approx(0.0)


# --- S45: estimators sample attempts, not per-KC evidence rows ---------------


def test_observations_collapses_the_per_kc_fan_out() -> None:
    """Every payload field an estimator reads (score, latency, hints, difficulty) is
    item-level and identical across the fan-out, so three rows would triple-weight one
    answer in pace, help-seeking, challenge, load, format and engagement estimates."""
    attempt = uuid.uuid4()
    events = [
        LearningEvent(
            learner_id=uuid.uuid4(),
            event_type="observation",
            attempt_id=attempt,
            payload={"score": 1.0, "latency_ms": 4200, "hints_used": 2},
        )
        for _ in range(3)
    ]

    assert len(_observations(events)) == 1


def test_observations_keeps_distinct_attempts_and_legacy_rows() -> None:
    learner = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    events = [
        LearningEvent(learner_id=learner, event_type="observation", attempt_id=a, payload={}),
        LearningEvent(learner_id=learner, event_type="observation", attempt_id=b, payload={}),
        # pre-migration rows carry no attempt_id and must not collapse into each other
        LearningEvent(learner_id=learner, event_type="observation", attempt_id=None, payload={}),
        LearningEvent(learner_id=learner, event_type="observation", attempt_id=None, payload={}),
        LearningEvent(learner_id=learner, event_type="placement_seed", attempt_id=None, payload={}),
    ]

    assert len(_observations(events)) == 4  # 2 attempts + 2 legacy; the seed is excluded
