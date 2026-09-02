"""Mine the LearningEvent log into per-(learner, KC) observation sequences (Phase 9b)."""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learning import LearningEvent
from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Step

log = structlog.get_logger(__name__)


async def mine_observation_sequences(
    session: AsyncSession, *, min_length: int = 3
) -> CalibrationDataset:
    """Group `observation` events into time-ordered (learner, KC) sequences of >= min_length steps.

    Rows are pulled ordered by (learner, KC, created_at), so each group's steps are already in
    time order and groups are contiguous. A malformed payload (missing/non-numeric score or
    difficulty) is skipped with a warning — never fatal.
    """
    stmt = (
        select(LearningEvent)
        .where(LearningEvent.event_type == "observation", LearningEvent.kc_id.isnot(None))
        .order_by(LearningEvent.learner_id, LearningEvent.kc_id, LearningEvent.created_at)
    )
    rows = (await session.scalars(stmt)).all()

    grouped: dict[tuple[str, str], list[Step]] = {}
    for event in rows:
        step = _step_of(event)
        if step is None:
            continue
        grouped.setdefault((str(event.learner_id), str(event.kc_id)), []).append(step)

    sequences = [
        ObservationSequence(learner_id=learner_id, kc_id=kc_id, steps=steps)
        for (learner_id, kc_id), steps in grouped.items()
        if len(steps) >= min_length
    ]
    return CalibrationDataset(sequences=sequences)


def _step_of(event: LearningEvent) -> Step | None:
    try:
        return Step(
            score=float(event.payload["score"]),
            difficulty=float(event.payload["difficulty"]),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("mine.skip_malformed_observation", event_id=str(event.id))
        return None
