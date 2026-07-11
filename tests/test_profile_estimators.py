"""Learner-profile dimension estimators: session clustering (shared helper) for now.

Per-dimension estimator tests are added alongside each estimator (see the commit sequence in
the learner-profile plan) — this file grows as `DIMENSION_SPECS` grows.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.learning.profile_estimators import _cluster_sessions
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
