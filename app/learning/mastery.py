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
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import DateTime, Float, Integer, and_, case, distinct, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import scheduler
from app.learning.assistance import evidence_credit
from app.learning.diagnosis import ACTIONABLE, FailureKind
from app.learning.tracer import Estimate, GlickoEstimator, MasteryEstimator, aggregate
from app.models.knowledge import KC, Topic
from app.models.learning import LearnerKCState, LearningEvent

_SECONDS_PER_DAY = 86_400.0

EVENT_SCHEMA_VERSION = 3
"""Payload shape of an ``observation`` event.

1 — score/difficulty/weight/credit and the grader's verdict.
2 — adds what an exact replay needs: the estimator's configuration, the timestamp the update
    actually used, the decay gap applied, the prediction made before the answer was seen, and
    the prior and posterior either side of the update. A version-1 row can still be scored, but
    it cannot be replayed exactly — it does not say what it was computed from.
"""

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

    ``hints_used`` and ``prior_attempts`` say how *assisted* the attempt was; together they
    scale the evidence down (see :mod:`app.learning.assistance`). ``prior_attempts`` counts
    earlier looks at this same question in this sitting, not lifetime practice — a review
    weeks later is an independent demonstration and counts fully.

    ``kc_scores`` is how a grader says the components did *differently* (S10). Without it the
    one ``score`` lands on every tagged KC, so a learner who set a least-squares problem up
    correctly and then botched the projection has the projection failure counted as evidence
    against every skill the question touched — including the ones they demonstrated. Only
    graders that can actually tell the components apart supply it; an MCQ has one outcome and
    cannot, so it stays ``None`` there and the aggregate applies, exactly as before.
    """

    learner_id: uuid.UUID
    kc_weights: dict[uuid.UUID, float]
    score: float = Field(ge=0.0, le=1.0)
    kc_scores: dict[uuid.UUID, float] | None = None
    difficulty: float = 0.0
    item_id: uuid.UUID | None = None
    response: dict | None = None
    latency_ms: int | None = None
    hints_used: int | None = None
    prior_attempts: int = Field(default=0, ge=0)
    attempt_id: uuid.UUID | None = None
    correct: bool | None = None
    detail: dict | None = None
    kc_diagnoses: dict[uuid.UUID, dict] | None = None
    """Per-KC structured diagnosis, already serialised (S09). Stored on the event so the
    reason an answer failed survives alongside the number, where the planner and any later
    analysis can reach it — a rationale that only ever reached the response body is a
    sentence nobody can query."""

    @field_validator("kc_weights")
    @classmethod
    def _weights_positive(cls, v: dict[uuid.UUID, float]) -> dict[uuid.UUID, float]:
        if not v:
            raise ValueError("an observation must tag at least one KC")
        if any(w <= 0.0 for w in v.values()):
            raise ValueError("KC weights must be positive")
        return v

    @model_validator(mode="after")
    def _component_scores_belong_to_this_item(self) -> "Observation":
        """A per-KC score for a KC the item is not tagged to is silently ignored downstream,
        which is the quiet kind of wrong — the grader believed it was marking something this
        answer covered. Rejected here instead."""
        if self.kc_scores is None:
            return self
        stray = set(self.kc_scores) - set(self.kc_weights)
        if stray:
            raise ValueError(
                f"kc_scores names KCs the item does not assess: {sorted(map(str, stray))}"
            )
        if any(not 0.0 <= v <= 1.0 for v in self.kc_scores.values()):
            raise ValueError("component scores must be within [0, 1]")
        return self


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


async def estimate_kcs(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kc_ids: Sequence[uuid.UUID],
    *,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> dict[uuid.UUID, Estimate]:
    """Current mastery for many KCs at once — one query, whatever the graph's size (S62).

    Every caller that wanted more than one estimate was fetching them one row at a time, and
    the rollups then fetched the same rows again to aggregate them. A KC with no state row
    still appears here, at the unknown prior: absent from the table is a fact about the
    learner, not a reason to leave it out of the answer.
    """
    now = now or datetime.now(UTC)
    if not kc_ids:
        return {}
    states = (
        await session.scalars(
            select(LearnerKCState).where(
                LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id.in_(kc_ids)
            )
        )
    ).all()
    by_kc = {
        state.kc_id: estimator.decay(
            _estimate_of(state), elapsed_days=_elapsed_days(state.last_seen_at, now)
        )
        for state in states
    }
    return {kc_id: by_kc.get(kc_id, Estimate()) for kc_id in kc_ids}


async def record_observation(
    session: AsyncSession,
    obs: Observation,
    *,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> list[LearnerKCState]:
    """Fold one graded interaction into every tagged KC and append per-KC events.

    Each KC's prior is first decayed to ``now`` (uncertainty grows with the gap since it
    was last seen), then updated with its apportioned share of the item's evidence — scaled
    down if the attempt was assisted (:mod:`app.learning.assistance`). One immutable
    ``learning_event`` is written per KC — the replayable, KC-tagged substrate the learner
    profile (§7.8) and DKT (§7.5) consume later.
    """
    now = now or datetime.now(UTC)
    total_w = sum(obs.kc_weights.values())
    # Hints and re-asks make this a weaker measurement of unaided ability, so it moves the
    # estimate less *and* shrinks its uncertainty less — the estimator's `weight` does both.
    credit = evidence_credit(hints_used=obs.hints_used, prior_attempts=obs.prior_attempts)
    # One id shared by this answer's whole per-KC fan-out, so consumers can tell "one learner
    # action tagged to three components" from "three separate attempts" (see LearningEvent).
    # A caller-supplied id doubles as an idempotency key, enforced by a unique index.
    attempt_id = obs.attempt_id or uuid.uuid4()
    updated: list[LearnerKCState] = []
    for kc_id, raw_w in obs.kc_weights.items():
        weight = raw_w / total_w
        # The score for *this* component where the grader could tell them apart, the item's
        # aggregate where it could not. Both are recorded below; this is the one the estimate
        # and the review schedule are built from, because both are per-KC facts.
        kc_score = obs.score if obs.kc_scores is None else obs.kc_scores.get(kc_id, obs.score)
        state = await _get_or_create_state(session, obs.learner_id, kc_id)
        elapsed_days = _elapsed_days(state.last_seen_at, now)
        decayed = estimator.decay(_estimate_of(state), elapsed_days=elapsed_days)
        # The model's belief *before* seeing this answer. Recorded rather than recomputed
        # later, so calibration measures what the learner was actually predicted to do
        # instead of what today's estimator would have predicted (S56).
        predicted = estimator.expected(decayed, difficulty=obs.difficulty)
        post = estimator.update(
            decayed, score=kc_score, difficulty=obs.difficulty, weight=weight * credit
        )
        state.ability = post.ability
        state.uncertainty = post.uncertainty
        state.last_seen_at = now
        # Advance FSRS retention scheduling for this KC and denormalize the next due date.
        state.fsrs_card, state.due_at = scheduler.review(state.fsrs_card, score=kc_score, now=now)
        session.add(
            LearningEvent(
                learner_id=obs.learner_id,
                kc_id=kc_id,
                event_type="observation",
                attempt_id=attempt_id,
                payload={
                    # This KC's own score. Every consumer of an observation event — the
                    # profile estimators, the evidence summary, replay — is asking a per-KC
                    # question, so the per-KC number is the one that belongs under this key.
                    "score": kc_score,
                    # What the item scored as a whole, kept so an answer can still be
                    # reassembled from its per-KC fan-out.
                    "item_score": obs.score,
                    # Whether `score` above is a *distinct* judgement of this component or
                    # just the item's aggregate landing on it. Without the flag a replay
                    # cannot tell the two apart, and would hand back a per-component
                    # breakdown for an MCQ that never had one — resolution invented after
                    # the fact, which is worse than none.
                    "component_scored": obs.kc_scores is not None,
                    "difficulty": obs.difficulty,
                    "weight": weight,
                    "item_id": str(obs.item_id) if obs.item_id is not None else None,
                    "response": obs.response,
                    "latency_ms": obs.latency_ms,
                    "hints_used": obs.hints_used,
                    "prior_attempts": obs.prior_attempts,
                    "credit": credit,
                    "correct": obs.correct,
                    "detail": obs.detail,
                    "diagnosis": (obs.kc_diagnoses or {}).get(kc_id),
                    "estimator": estimator.name,
                    # Everything a replay needs to reproduce this step exactly (S56).
                    # `observed_at` rather than the row's `created_at`: `created_at` is the
                    # transaction's clock, so a batch written together ties, and the decay gap
                    # inferred from it would be zero for every step.
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "estimator_config": estimator.config,
                    "observed_at": now.isoformat(),
                    "elapsed_days": elapsed_days,
                    "predicted": predicted,
                    "prior_ability": decayed.ability,
                    "prior_uncertainty": decayed.uncertainty,
                    "posterior_ability": post.ability,
                    "posterior_uncertainty": post.uncertainty,
                },
            )
        )
        updated.append(state)
    await session.flush()
    return updated


async def recent_attempts_at_item(
    session: AsyncSession,
    learner_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    now: datetime | None = None,
    within_minutes: int | None = None,
) -> int:
    """How many times this learner already attempted this item in the current sitting.

    Repeat exposure only costs evidence while the question is still fresh: re-answering
    something you were just told you got wrong is not an independent demonstration, but
    meeting it again weeks later is exactly the retention practice FSRS schedules. The window
    is therefore the same "one sitting" gap the learner profile uses.

    Counts *attempts*, not rows — one answer writes one event per tagged KC. Rows predating
    ``attempt_id`` (and any whose KC was since deleted) each stand alone, matching how the
    rest of the engine reads an unattributed row.

    ``LearningEvent.created_at`` is a naive ``TIMESTAMP`` written by Postgres's ``now()``,
    which is UTC in this deployment, so the cutoff drops tzinfo — the same convention as
    ``services.analytics.get_activity``.
    """
    now = now or datetime.now(UTC)
    minutes = (
        within_minutes if within_minutes is not None else get_settings().profile_session_gap_minutes
    )
    since = (now - timedelta(minutes=minutes)).replace(tzinfo=None)
    return (
        await session.scalar(
            select(func.count(distinct(func.coalesce(LearningEvent.attempt_id, LearningEvent.id))))
            .select_from(LearningEvent)
            .where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.event_type == "observation",
                LearningEvent.created_at >= since,
                LearningEvent.payload["item_id"].astext == str(item_id),
            )
        )
        or 0
    )


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
                # A replay starts from this, not from the population prior — which is what
                # made replayed sequences for placed learners diverge from the first step.
                "schema_version": EVENT_SCHEMA_VERSION,
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


class Struggle(BaseModel):
    """How badly, and why, a learner is currently stuck on one component (S11).

    ``consecutive_failures`` counts back from the most recent attempt and stops at the first
    one that went well — a learner who failed twice and then succeeded is not stuck, and a
    lifetime tally would say they were forever.
    """

    consecutive_failures: int = 0
    diagnosed_prerequisite: str = ""
    """The prerequisite the grader named in the most recent attempt that blamed one (S09).
    Free text, and not resolved to a KC here: the planner owns the graph."""


async def recent_struggle(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kc_id: uuid.UUID,
    *,
    threshold: float,
    limit: int = 10,
) -> Struggle:
    """Read the learner's recent run of attempts at ``kc_id``, newest first.

    Ordered by ``observed_at``, not ``created_at``, for the reason S56 recorded: `created_at`
    is the transaction's clock, so a batch written together ties and the "most recent"
    attempt would be whichever row the scan happened to reach first.
    """
    when = func.coalesce(
        LearningEvent.payload["observed_at"].astext.cast(DateTime), LearningEvent.created_at
    )
    rows = (
        await session.execute(
            select(
                LearningEvent.payload["score"].astext.cast(Float),
                LearningEvent.payload["diagnosis"],
            )
            .where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.kc_id == kc_id,
                LearningEvent.event_type == "observation",
            )
            .order_by(when.desc(), LearningEvent.id.desc())
            .limit(limit)
        )
    ).all()

    failures = 0
    for score, _ in rows:
        if score is None or score >= threshold:
            break
        failures += 1

    named = ""
    for _, raw in rows:
        if isinstance(raw, dict) and raw.get("kind") == FailureKind.PREREQUISITE.value:
            named = str(raw.get("prerequisite", "")).strip()
            if named:
                break
    return Struggle(consecutive_failures=failures, diagnosed_prerequisite=named)


async def prior_failure_kinds(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kc_ids: Sequence[uuid.UUID],
    *,
    limit: int = 20,
) -> dict[uuid.UUID, dict[FailureKind, int]]:
    """How often each failure kind has already come up per component (S09).

    A diagnosis was a property of one attempt and nothing joined them up, so a misconception
    recurring five times was indistinguishable from five unrelated slips — and those want
    opposite responses. The fifth repetition of one wrong idea means the explanation is not
    working and the approach has to change; five different slips mean the learner is basically
    fine and having a bad run.

    This is deliberately a **count, not a confidence**. The per-diagnosis ``confidence`` is the
    model's own and is not calibrated, so nothing may gate on it (``app.learning.diagnosis``).
    Recurrence is evidence of a different kind: the same label arising independently across
    separate attempts, on separate items, graded in separate calls. It costs nothing to compute
    and it does not ask the model to be right about how sure it is.

    Ordered by ``observed_at`` for the reason S56 recorded — ``created_at`` is the transaction
    clock — and capped per component, so a learner with years of history pays a bounded read.
    """
    if not kc_ids:
        return {}
    when = func.coalesce(
        LearningEvent.payload["observed_at"].astext.cast(DateTime), LearningEvent.created_at
    )
    counts: dict[uuid.UUID, dict[FailureKind, int]] = {}
    for kc_id in kc_ids:
        rows = (
            await session.execute(
                select(LearningEvent.payload["diagnosis"])
                .where(
                    LearningEvent.learner_id == learner_id,
                    LearningEvent.kc_id == kc_id,
                    LearningEvent.event_type == "observation",
                )
                .order_by(when.desc(), LearningEvent.id.desc())
                .limit(limit)
            )
        ).all()
        per_kc: dict[FailureKind, int] = {}
        for (raw,) in rows:
            if not isinstance(raw, dict):
                continue
            try:
                kind = FailureKind(str(raw.get("kind")))
            except ValueError:
                continue  # a vocabulary that has since changed is not a reason to fail
            if kind in ACTIONABLE:
                per_kc[kind] = per_kc.get(kind, 0) + 1
        if per_kc:
            counts[kc_id] = per_kc
    return counts


DETOUR_EVENT = "detour"
"""``LearningEvent.event_type`` for a prerequisite detour being taken (S11).

Tagged to the **blocked** component rather than the prerequisite the learner is sent to,
because the question this record exists to answer is "did detouring help *this* component" —
the prerequisite is where they went, not what was stuck. Every other reader of the event log
filters on ``"observation"`` explicitly, so this adds a row type without changing what any of
them see.
"""


def record_detour(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    blocked_kc_id: uuid.UUID,
    prereq_kc_id: uuid.UUID,
    reason: str,
    consecutive_failures: int,
) -> None:
    """Record that a learner was sent to ``prereq_kc_id`` before ``blocked_kc_id``.

    Detours were a plan mutation and nothing else: the step appeared, the step closed, and no
    trace survived that a decision had been made. "Does detouring help?" is precisely the kind
    of question S59 exists to ask, and it could not be asked of the data at all — there was no
    data. Added to the session, not committed: it belongs to the same transaction as the plan
    revision that caused it, so a rolled-back revision does not leave a detour on the record
    that never happened.
    """
    session.add(
        LearningEvent(
            learner_id=learner_id,
            kc_id=blocked_kc_id,
            event_type=DETOUR_EVENT,
            payload={
                "prereq_kc_id": str(prereq_kc_id),
                "reason": reason,
                "consecutive_failures": consecutive_failures,
            },
        )
    )


async def detour_attempts(
    session: AsyncSession, learner_id: uuid.UUID, blocked_kc_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    """How many times this learner has been detoured to each prerequisite of one component.

    What the cap is read from: a prerequisite the learner has already been sent to twice, and
    is still failing the blocked component after, is not the blocker — or detouring to it is
    not the remedy. Either way a third trip is not the answer, and without this count nothing
    stopped the same detour firing on every failed attempt forever.
    """
    rows = (
        await session.execute(
            select(LearningEvent.payload["prereq_kc_id"].astext).where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.kc_id == blocked_kc_id,
                LearningEvent.event_type == DETOUR_EVENT,
            )
        )
    ).all()
    counts: dict[uuid.UUID, int] = {}
    for (raw,) in rows:
        try:
            kc_id = uuid.UUID(str(raw))
        except (TypeError, ValueError):
            continue  # a payload written by hand or by a future shape; not a reason to fail
        counts[kc_id] = counts.get(kc_id, 0) + 1
    return counts


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
    estimates = await estimate_kcs(session, learner_id, kc_ids, now=now, estimator=estimator)
    return aggregate(list(estimates.values()))


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
    # Three queries, not three per topic: the whole subject's KCs, then every state row for
    # them, then the aggregation in memory (S62). The rollup used to re-read each topic's KCs
    # and each KC's state a second time, having already read both to build the topic estimate.
    rows = (
        await session.execute(
            select(KC.id, KC.topic_id)
            .join(Topic, Topic.id == KC.topic_id)
            .where(Topic.subject_id == subject_id)
        )
    ).all()
    kcs_by_topic: dict[uuid.UUID, list[uuid.UUID]] = {}
    for kc_id, topic_id in rows:
        kcs_by_topic.setdefault(topic_id, []).append(kc_id)
    estimates_by_kc = await estimate_kcs(
        session, learner_id, [kc_id for kc_id, _ in rows], now=now, estimator=estimator
    )
    estimates: list[Estimate] = []
    weights: list[float] = []
    for kc_ids in kcs_by_topic.values():
        estimates.append(aggregate([estimates_by_kc[kc_id] for kc_id in kc_ids]))
        weights.append(float(len(kc_ids)))
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


class KCEvidence(BaseModel):
    """What a KC's mastery estimate actually rests on (S14).

    The estimate is one number and cannot say whether it came from solving four different
    problems unaided over three weeks or from answering the same question four times in ten
    minutes after being told the answer. Those are not the same claim about a learner, and
    only one of them is what "mastered" is meant to mean.

    Counting is over the KC-tagged event log, so it needs no new table and covers history
    recorded before this existed.
    """

    kc_id: uuid.UUID
    attempts: int
    # Distinct items, and distinct items answered with no hint and no earlier look at that
    # same question in the sitting — the assistance signal S13 already records.
    distinct_items: int
    unassisted_items: int
    # Days between the first attempt at this KC and the most recent *unassisted* one. None
    # when nothing here was ever answered unaided.
    span_days: float | None

    @property
    def transfer_shown(self) -> bool:
        """Solved more than one different problem for this KC, unaided."""
        return self.unassisted_items >= 2

    def retention_shown(self, *, min_days: float) -> bool:
        """Demonstrated unaided at least ``min_days`` after first meeting the component."""
        return self.span_days is not None and self.span_days >= min_days


async def kc_evidence(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, KCEvidence]:
    """Evidence quality for many KCs in one query.

    One query for the whole set, like ``estimate_kcs``: this is read alongside a subject's
    mastery roll-up, and a per-KC call there would put the page back where S62 found it.
    KCs with no observations are absent from the result rather than present and empty — the
    caller already knows which it asked for, and "no evidence" is not a row.
    """
    if not kc_ids:
        return {}
    # An attempt is unassisted when it used no hints and was not a re-look at the same
    # question in the same sitting. Both are recorded per event by ``record_observation``.
    unassisted = and_(
        func.coalesce(LearningEvent.payload["hints_used"].astext.cast(Integer), 0) == 0,
        func.coalesce(LearningEvent.payload["prior_attempts"].astext.cast(Integer), 0) == 0,
    )
    item = LearningEvent.payload["item_id"].astext
    # `observed_at` and not `created_at`, for the reason S56 records: `created_at` is the
    # transaction's clock, so a batch written together ties and an event recorded with an
    # explicit time does not match it at all. Older rows have no `observed_at` and fall back.
    when = func.coalesce(
        LearningEvent.payload["observed_at"].astext.cast(DateTime), LearningEvent.created_at
    )
    rows = await session.execute(
        select(
            LearningEvent.kc_id,
            func.count(distinct(func.coalesce(LearningEvent.attempt_id, LearningEvent.id))),
            func.count(distinct(item)),
            func.count(distinct(case((unassisted, item)))),
            func.min(when),
            func.max(case((unassisted, when))),
        )
        .where(
            LearningEvent.learner_id == learner_id,
            LearningEvent.event_type == "observation",
            LearningEvent.kc_id.in_(kc_ids),
        )
        .group_by(LearningEvent.kc_id)
    )
    out: dict[uuid.UUID, KCEvidence] = {}
    for kc_id, attempts, items, unassisted_items, first_at, last_unassisted_at in rows:
        if kc_id is None:
            continue
        span = None
        if first_at is not None and last_unassisted_at is not None:
            span = (last_unassisted_at - first_at).total_seconds() / _SECONDS_PER_DAY
        out[kc_id] = KCEvidence(
            kc_id=kc_id,
            attempts=int(attempts or 0),
            distinct_items=int(items or 0),
            unassisted_items=int(unassisted_items or 0),
            span_days=span,
        )
    return out
