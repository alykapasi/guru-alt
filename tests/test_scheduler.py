"""Unit tests for the FSRS scheduling seam (pure, no DB)."""

import json
from datetime import UTC, datetime

from fsrs import Rating

from app.learning.scheduler import rating_for, review


def test_rating_for_spans_the_scale() -> None:
    assert rating_for(0.0) is Rating.Again
    assert rating_for(0.49) is Rating.Again
    assert rating_for(0.5) is Rating.Hard
    assert rating_for(0.74) is Rating.Hard
    assert rating_for(0.75) is Rating.Good
    assert rating_for(0.94) is Rating.Good
    assert rating_for(0.95) is Rating.Easy
    assert rating_for(1.0) is Rating.Easy


def test_review_from_scratch_schedules_future_due() -> None:
    now = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    card, due = review(None, score=0.9, now=now)
    assert due > now
    assert isinstance(card, dict)
    json.dumps(card)  # must be JSONB-serializable (no raise)


def test_review_resumes_a_stored_card() -> None:
    now = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    first, _ = review(None, score=0.9, now=now)
    later = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    second, due = review(first, score=0.9, now=later)
    # A second successful recall pushes the next review further out than the first interval.
    assert due > later
    assert second["stability"] >= first["stability"]


def test_failed_recall_schedules_sooner_than_success() -> None:
    now = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    _, due_fail = review(None, score=0.1, now=now)
    _, due_pass = review(None, score=1.0, now=now)
    assert due_fail < due_pass
