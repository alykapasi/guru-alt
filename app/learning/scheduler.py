"""FSRS retention scheduling behind a thin seam (TECHNICAL_DESIGN §7.2, §11).

Each graded recall advances a per-KC FSRS memory card and yields the next ``due`` instant.
The card is persisted *opaquely* — its serialized dict on ``LearnerKCState.fsrs_card`` —
so our schema never couples to FSRS internals (stability/difficulty/state live inside the
blob). The only thing the rest of the engine reads is the denormalized ``due_at``.

The score→rating map is the bridge from our continuous grade in [0, 1] to FSRS's 4-point
recall scale; thresholds are an empirical starting point, not a contract (cf. §7.3).
"""

from datetime import datetime
from typing import Any, cast

from fsrs import Card, Rating, Scheduler

_scheduler = Scheduler()
"""Default FSRS parameters. Per-deployment tuning (or per-learner) comes later."""


def rating_for(score: float) -> Rating:
    """Map a continuous grade in [0, 1] to FSRS's recall rating."""
    if score < 0.5:
        return Rating.Again
    if score < 0.75:
        return Rating.Hard
    if score < 0.95:
        return Rating.Good
    return Rating.Easy


def review(card_data: dict | None, *, score: float, now: datetime) -> tuple[dict, datetime]:
    """Advance the FSRS card with one graded recall.

    ``card_data`` is the previously-stored serialized card (``None`` for a KC's first
    review). Returns the new serialized card and its next ``due`` instant.
    """
    # FSRS speaks its own ``CardDict`` TypedDict; we persist (and accept) an opaque JSONB
    # ``dict`` and translate at this boundary.
    card = Card.from_dict(cast(Any, card_data)) if card_data else Card()
    reviewed, _log = _scheduler.review_card(card, rating_for(score), review_datetime=now)
    return dict(reviewed.to_dict()), reviewed.due
