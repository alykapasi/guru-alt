"""Mine the LearningEvent log into per-(learner, KC) observation sequences (Phase 9b).

Extended by S56: a mined sequence now carries the placement prior it started from and, per
step, the weight, assistance credit, decay gap and prediction production actually used — so a
replay reproduces the stored state instead of approximating it.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learning import LearningEvent
from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Seed, Step

log = structlog.get_logger(__name__)


async def mine_observation_sequences(
    session: AsyncSession, *, min_length: int = 3
) -> CalibrationDataset:
    """Group `observation` events into time-ordered (learner, KC) sequences of >= min_length steps.

    Rows are pulled ordered by (learner, KC, created_at), so each group's steps are already in
    time order and groups are contiguous. A malformed payload (missing/non-numeric score or
    difficulty) is skipped with a warning — never fatal.

    ``placement_seed`` events are read in the same pass. A seeded KC starts from its seed, not
    from the population prior; ``seed_prior`` refuses to write over existing state, so a seed is
    by construction the state the first observation updated.

    Steps are re-sorted within a group by the ``observed_at`` the update itself used, when every
    step in the group carries one. ``created_at`` is the transaction's clock: several
    observations committed together share it exactly, and one recorded with an explicit ``now``
    (a backfill, a replay, a test) does not match it at all. Ordering a learner's history by it
    is therefore only approximately right, and silently wrong in exactly the cases where the
    order matters most.
    """
    stmt = (
        select(LearningEvent)
        .where(
            LearningEvent.event_type.in_(("observation", "placement_seed")),
            LearningEvent.kc_id.isnot(None),
        )
        .order_by(LearningEvent.learner_id, LearningEvent.kc_id, LearningEvent.created_at)
    )
    rows = (await session.scalars(stmt)).all()

    grouped: dict[tuple[str, str], list[tuple[str | None, Step]]] = {}
    seeds: dict[tuple[str, str], Seed] = {}
    estimators: dict[tuple[str, str], tuple[str | None, dict[str, float] | None]] = {}

    for event in rows:
        key = (str(event.learner_id), str(event.kc_id))
        if event.event_type == "placement_seed":
            seed = _seed_of(event)
            if seed is not None:
                seeds[key] = seed
            continue
        step = _step_of(event)
        if step is None:
            continue
        grouped.setdefault(key, []).append((_str_or_none(event.payload, "observed_at"), step))
        estimators.setdefault(key, (_str_or_none(event.payload, "estimator"), _config_of(event)))

    sequences = []
    for key, entries in grouped.items():
        steps = _ordered(entries)
        if len(steps) < min_length:
            continue
        estimator, config = estimators.get(key, (None, None))
        sequences.append(
            ObservationSequence(
                learner_id=key[0],
                kc_id=key[1],
                steps=steps,
                seed=seeds.get(key),
                estimator=estimator,
                estimator_config=config,
            )
        )
    return CalibrationDataset(sequences=sequences)


def _ordered(entries: list[tuple[str | None, Step]]) -> list[Step]:
    """Steps in the order production applied them.

    Only re-sorts when every step says when it happened; a partially migrated group keeps the
    row order it arrived in, which is the best the older rows can support. ISO-8601 strings
    from a single writer sort correctly as strings, and `sorted` is stable, so steps sharing a
    timestamp keep their row order.
    """
    if any(when is None for when, _ in entries):
        return [step for _, step in entries]
    return [step for _, step in sorted(entries, key=lambda e: e[0] or "")]


def _float_or_none(payload: dict, key: str) -> float | None:
    try:
        value = payload[key]
    except (KeyError, TypeError):
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(payload: dict, key: str) -> str | None:
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _config_of(event: LearningEvent) -> dict[str, float] | None:
    raw = event.payload.get("estimator_config") if isinstance(event.payload, dict) else None
    if not isinstance(raw, dict):
        return None
    config = {k: _float_or_none(raw, k) for k in raw}
    if any(v is None for v in config.values()):
        return None
    return {k: v for k, v in config.items() if v is not None}


def _seed_of(event: LearningEvent) -> Seed | None:
    ability = _float_or_none(event.payload, "ability")
    uncertainty = _float_or_none(event.payload, "uncertainty")
    if ability is None or uncertainty is None or uncertainty <= 0.0:
        log.warning("mine.skip_malformed_seed", event_id=str(event.id))
        return None
    return Seed(
        ability=ability,
        uncertainty=uncertainty,
        source=_str_or_none(event.payload, "source") or "unknown",
    )


def _step_of(event: LearningEvent) -> Step | None:
    score = _float_or_none(event.payload, "score")
    difficulty = _float_or_none(event.payload, "difficulty")
    if score is None or difficulty is None or not 0.0 <= score <= 1.0:
        log.warning("mine.skip_malformed_observation", event_id=str(event.id))
        return None
    return Step(
        score=score,
        difficulty=difficulty,
        weight=_float_or_none(event.payload, "weight"),
        credit=_float_or_none(event.payload, "credit"),
        elapsed_days=_float_or_none(event.payload, "elapsed_days"),
        predicted=_float_or_none(event.payload, "predicted"),
        prior_ability=_float_or_none(event.payload, "prior_ability"),
        prior_uncertainty=_float_or_none(event.payload, "prior_uncertainty"),
        posterior_ability=_float_or_none(event.payload, "posterior_ability"),
        posterior_uncertainty=_float_or_none(event.payload, "posterior_uncertainty"),
    )
