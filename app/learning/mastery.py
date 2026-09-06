"""DB-backed knowledge tracer: persist per-KC mastery and roll it up the graph.

This wraps the pure ``GlickoEstimator`` (see :mod:`app.learning.tracer`) with the I/O the
estimator deliberately avoids: loading/saving ``LearnerKCState``, applying time-decay
against a real clock, apportioning a multi-KC item's credit across its KCs, appending the
KC-tagged ``learning_events`` log, and rolling estimates up KC → Topic → Subject
(TECHNICAL_DESIGN §7.2-7.5).

Mutators ``flush`` so assigned ids and updated state are visible within the transaction,
but leave ``commit`` to the caller — the answer-an-item endpoint (slice 3) commits grade +
tracer update + event together so an interaction is recorded atomically.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import scheduler
from app.learning.tracer import Estimate, GlickoEstimator, MasteryEstimator, aggregate
from app.models.knowledge import KC, Topic
from app.models.learning import LearnerKCState, LearningEvent

_SECONDS_PER_DAY = 86_400.0

DEFAULT_ESTIMATOR: MasteryEstimator = GlickoEstimator()
"""The estimator the engine runs today. Swapping it (→ DKT) touches only this binding."""


class Observation(BaseModel):
    """One graded interaction, ready to fold into the tracer (§7.2).

    ``kc_weights`` maps each tagged KC to its relative credit; a single-KC item is just one
    entry. The item is treated as one unit of evidence split across the KCs by weight.

    ``attempt_id``, when the caller supplies one, is an idempotency key: the same id
    submitted twice records the interaction once (see ``services.assessment.answer_item``).
    Left unset, one is generated per observation. ``correct``/``detail`` carry the grader's
    verdict into the log so a recorded attempt can be replayed without re-grading it.
    """

    learner_id: uuid.UUID
    kc_weights: dict[uuid.UUID, float]
    score: float = Field(ge=0.0, le=1.0)
    difficulty: float = 0.0
    item_id: uuid.UUID | None = None
    response: dict | None = None
    latency_ms: int | None = None
    hints_used: int | None = None
    attempt_id: uuid.UUID | None = None
    correct: bool | None = None
    detail: dict | None = None

    @field_validator("kc_weights")
    @classmethod
    def _weights_positive(cls, v: dict[uuid.UUID, float]) -> dict[uuid.UUID, float]:
        if not v:
            raise ValueError("an observation must tag at least one KC")
        if any(w <= 0.0 for w in v.values()):
            raise ValueError("KC weights must be positive")
        return v


class ReviewItem(BaseModel):
    """A KC whose FSRS-scheduled review has come due (the tracer's ``due_reviews`` output)."""

    model_config = ConfigDict(from_attributes=True)

    kc_id: uuid.UUID
    due_at: datetime
    ability: float
    uncertainty: float


def _estimate_of(state: LearnerKCState) -> Estimate:
    return Estimate(ability=state.ability, uncertainty=state.uncertainty)


def _elapsed_days(last_seen: datetime | None, now: datetime) -> float:
    if last_seen is None:
        return 0.0
    return max((now - last_seen).total_seconds() / _SECONDS_PER_DAY, 0.0)


async def _get_or_create_state(
    session: AsyncSession, learner_id: uuid.UUID, kc_id: uuid.UUID
) -> LearnerKCState:
    """The learner's state row for this KC, creating a default one on first sighting.

    Creation goes through ``ON CONFLICT DO NOTHING`` against the (learner, KC) unique
    constraint: two answers arriving together on a KC the learner has never been assessed
    on would both read "no state" and both insert, and one would fail the whole
    transaction. Losing the race here is not an error — it just means someone else created
    the row, so re-read it.
    """
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id == kc_id
        )
    )
    if state is not None:
        return state
    await session.execute(
        pg_insert(LearnerKCState)
        .values(learner_id=learner_id, kc_id=kc_id)
        .on_conflict_do_nothing(index_elements=["learner_id", "kc_id"])
    )
    created = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id == kc_id
        )
    )
    assert created is not None  # the row exists now: we inserted it, or the other writer did
    return created


async def estimate_kc(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kc_id: uuid.UUID,
    *,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> Estimate:
    """Current mastery for one KC, with uncertainty decayed to ``now``. Read-only.

    An unseen KC returns the unknown prior (ability 0, wide uncertainty).
    """
    now = now or datetime.now(UTC)
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id == kc_id
        )
    )
    if state is None:
        return Estimate()
    return estimator.decay(_estimate_of(state), elapsed_days=_elapsed_days(state.last_seen_at, now))


async def record_observation(
    session: AsyncSession,
    obs: Observation,
    *,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> list[LearnerKCState]:
    """Fold one graded interaction into every tagged KC and append per-KC events.

    Each KC's prior is first decayed to ``now`` (uncertainty grows with the gap since it
    was last seen), then updated with its apportioned share of the item's evidence. One
    immutable ``learning_event`` is written per KC — the replayable, KC-tagged substrate
    the learner profile (§7.8) and DKT (§7.5) consume later.
    """
    now = now or datetime.now(UTC)
    total_w = sum(obs.kc_weights.values())
    # One id shared by this answer's whole per-KC fan-out, so consumers can tell "one learner
    # action tagged to three components" from "three separate attempts" (see LearningEvent).
    # A caller-supplied id doubles as an idempotency key, enforced by a unique index.
    attempt_id = obs.attempt_id or uuid.uuid4()
    updated: list[LearnerKCState] = []
    for kc_id, raw_w in obs.kc_weights.items():
        weight = raw_w / total_w
        state = await _get_or_create_state(session, obs.learner_id, kc_id)
        decayed = estimator.decay(
            _estimate_of(state), elapsed_days=_elapsed_days(state.last_seen_at, now)
        )
        post = estimator.update(decayed, score=obs.score, difficulty=obs.difficulty, weight=weight)
        state.ability = post.ability
        state.uncertainty = post.uncertainty
        state.last_seen_at = now
        # Advance FSRS retention scheduling for this KC and denormalize the next due date.
        state.fsrs_card, state.due_at = scheduler.review(state.fsrs_card, score=obs.score, now=now)
        session.add(
            LearningEvent(
                learner_id=obs.learner_id,
                kc_id=kc_id,
                event_type="observation",
                attempt_id=attempt_id,
                payload={
                    "score": obs.score,
                    "difficulty": obs.difficulty,
                    "weight": weight,
                    "item_id": str(obs.item_id) if obs.item_id is not None else None,
                    "response": obs.response,
                    "latency_ms": obs.latency_ms,
                    "hints_used": obs.hints_used,
                    "correct": obs.correct,
                    "detail": obs.detail,
                    "estimator": estimator.name,
                },
            )
        )
        updated.append(state)
    await session.flush()
    return updated


async def seed_prior(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kc_id: uuid.UUID,
    estimate: Estimate,
    *,
    source: str = "placement",
) -> LearnerKCState | None:
    """Set an initial ability/uncertainty for a KC that has no evidence yet.

    Unlike ``record_observation``, this sets a state directly rather than Bayesian-updating
    a prior — there's no real interaction to weigh, just a soft signal (e.g. placement
    inference). Only writes if the learner has no existing state for this KC, so it never
    overwrites real evidence (from an answered item or an earlier seed). Logs a
    ``LearningEvent`` like any other tracer write, for replayability.
    """
    existing = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id == kc_id
        )
    )
    if existing is not None:
        return None
    state = LearnerKCState(
        learner_id=learner_id,
        kc_id=kc_id,
        ability=estimate.ability,
        uncertainty=estimate.uncertainty,
    )
    session.add(state)
    session.add(
        LearningEvent(
            learner_id=learner_id,
            kc_id=kc_id,
            event_type="placement_seed",
            payload={
                "ability": estimate.ability,
                "uncertainty": estimate.uncertainty,
                "source": source,
            },
        )
    )
    await session.flush()
    return state


async def due_reviews(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[ReviewItem]:
    """KCs whose FSRS review has come due (``due_at <= now``), soonest-due first.

    This is the retention half of the tracer: the lesson-plan policy interleaves these
    into new material so knowledge stays durable (§7.7). Capped at ``settings.due_reviews_limit``
    by default — a real (if generous) bound, distinct from ``reviews_due_item_limit`` which
    separately bounds how many of these get an item eagerly resolved.
    """
    now = now or datetime.now(UTC)
    limit = limit if limit is not None else get_settings().due_reviews_limit
    states = (
        await session.scalars(
            select(LearnerKCState)
            .where(
                LearnerKCState.learner_id == learner_id,
                LearnerKCState.due_at.is_not(None),
                LearnerKCState.due_at <= now,
            )
            .order_by(LearnerKCState.due_at)
            .limit(limit)
        )
    ).all()
    return [ReviewItem.model_validate(s) for s in states]


async def rollup_topic(
    session: AsyncSession,
    learner_id: uuid.UUID,
    topic_id: uuid.UUID,
    *,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> Estimate:
    """Aggregate every KC in a topic into one estimate (equal coverage weight per KC)."""
    now = now or datetime.now(UTC)
    kc_ids = (await session.scalars(select(KC.id).where(KC.topic_id == topic_id))).all()
    estimates = [
        await estimate_kc(session, learner_id, kc_id, now=now, estimator=estimator)
        for kc_id in kc_ids
    ]
    return aggregate(estimates)


async def rollup_subject(
    session: AsyncSession,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID,
    *,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> Estimate:
    """Aggregate a subject's topics, weighting each topic by its KC count (coverage)."""
    now = now or datetime.now(UTC)
    topic_ids = (
        await session.scalars(select(Topic.id).where(Topic.subject_id == subject_id))
    ).all()
    estimates: list[Estimate] = []
    weights: list[float] = []
    for topic_id in topic_ids:
        kc_count = await session.scalar(
            select(func.count()).select_from(KC).where(KC.topic_id == topic_id)
        )
        if not kc_count:
            continue
        estimates.append(
            await rollup_topic(session, learner_id, topic_id, now=now, estimator=estimator)
        )
        weights.append(float(kc_count))
    return aggregate(estimates, weights)


@runtime_checkable
class KnowledgeTracer(Protocol):
    """The stateful, DB-backed tracer seam (TECHNICAL_DESIGN §7.2).

    Three operations: ``estimate`` a KC's current mastery, ``update`` from one graded
    interaction (mastery + FSRS scheduling + event log), and list ``due_reviews``. The
    *pure* mastery math sits one layer down behind :class:`MasteryEstimator`; swapping
    Elo/Glicko → DKT means a new implementation of *this* interface, not new call sites.
    """

    name: str

    async def estimate(
        self, session: AsyncSession, learner_id: uuid.UUID, kc_id: uuid.UUID
    ) -> Estimate: ...

    async def update(self, session: AsyncSession, obs: Observation) -> Sequence[LearnerKCState]: ...

    async def due_reviews(
        self, session: AsyncSession, learner_id: uuid.UUID
    ) -> Sequence[ReviewItem]: ...


class GlickoTracer:
    """The current ``KnowledgeTracer``: continuous Elo/Glicko mastery + FSRS retention.

    A thin facade over this module's functions, holding the swappable estimator — the same
    Protocol-plus-default-instance shape as :class:`MasteryEstimator` / ``DEFAULT_ESTIMATOR``.
    """

    name = "glicko+fsrs"

    def __init__(self, estimator: MasteryEstimator = DEFAULT_ESTIMATOR) -> None:
        self._estimator = estimator

    async def estimate(
        self, session: AsyncSession, learner_id: uuid.UUID, kc_id: uuid.UUID
    ) -> Estimate:
        return await estimate_kc(session, learner_id, kc_id, estimator=self._estimator)

    async def update(self, session: AsyncSession, obs: Observation) -> list[LearnerKCState]:
        return await record_observation(session, obs, estimator=self._estimator)

    async def due_reviews(self, session: AsyncSession, learner_id: uuid.UUID) -> list[ReviewItem]:
        return await due_reviews(session, learner_id)


DEFAULT_TRACER: KnowledgeTracer = GlickoTracer()
"""The tracer the engine runs today. Swapping it (→ DKT) touches only this binding."""
