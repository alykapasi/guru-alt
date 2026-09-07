"""The learner profile: DB-facing orchestration around the estimator catalog.

Mirrors ``mastery.py``'s split from ``tracer.py`` — the pure/LLM estimation logic lives in
``app.learning.profile_estimators``; this module owns the I/O (loading history, upserting
dimension rows, logging LLM cost, committing).

Refresh is **on-demand**, not triggered on every graded answer: recomputing a dozen
dimensions (three of which call an LLM) after every single item would slow down the answer
endpoint and burn tokens for no reason. A client calls ``POST /profile/refresh`` when it wants
an up-to-date view (dashboard load, end of session) — the same "synchronous compute on demand"
shape as the placement diagnostic, not the tracer's "update on every observation."
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.profile_estimators import (
    DIMENSION_SPECS,
    PROFILE_LLM_ROLE,
    DimensionEstimate,
    DimensionSpec,
    EstimatorContext,
)
from app.llm import LLMClient
from app.models.chat import Conversation, Message
from app.models.learning import LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.profile import LearnerProfile, ProfileDimension
from app.services.llm_log import log_llm_call


async def _load_events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        (
            await session.scalars(
                select(LearningEvent)
                .where(LearningEvent.learner_id == learner_id)
                .order_by(LearningEvent.created_at)
            )
        ).all()
    )


async def _load_own_messages(session: AsyncSession, learner_id: uuid.UUID) -> list[Message]:
    return list(
        (
            await session.scalars(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.learner_id == learner_id, Message.role == "user")
                .order_by(Message.created_at)
            )
        ).all()
    )


async def _ensure_profile(session: AsyncSession, learner_id: uuid.UUID) -> LearnerProfile:
    profile = await session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner_id)
    )
    if profile is None:
        profile = LearnerProfile(learner_id=learner_id)
        session.add(profile)
        await session.flush()
    return profile


async def _upsert_dimension(
    session: AsyncSession,
    learner_id: uuid.UUID,
    spec: DimensionSpec,
    result: DimensionEstimate,
) -> None:
    dim = await session.scalar(
        select(ProfileDimension).where(
            ProfileDimension.learner_id == learner_id, ProfileDimension.key == spec.key
        )
    )
    if dim is None:
        session.add(
            ProfileDimension(
                learner_id=learner_id,
                key=spec.key,
                value=result.value,
                uncertainty=result.uncertainty,
                kind=spec.kind,
                source=spec.source,
            )
        )
        return
    dim.value = result.value
    dim.uncertainty = result.uncertainty
    dim.kind = spec.kind
    dim.source = spec.source


async def refresh_profile(
    session: AsyncSession, learner_id: uuid.UUID, llm: LLMClient
) -> list[ProfileDimension]:
    """Recompute every dimension in the catalog from the learner's current history.

    Estimators that return ``None`` (not enough evidence yet) leave that dimension untouched —
    a thin history means fewer dimensions computed, never a fabricated confident value.
    """
    await _ensure_profile(session, learner_id)
    context = EstimatorContext(
        session=session,
        learner_id=learner_id,
        events=await _load_events(session, learner_id),
        messages=await _load_own_messages(session, learner_id),
        llm=llm,
    )
    for spec in DIMENSION_SPECS:
        result, usage = await spec.estimate(context)
        if usage.total_tokens:
            await log_llm_call(
                learner_id=learner_id,
                role=PROFILE_LLM_ROLE.value,
                spec=llm.spec(PROFILE_LLM_ROLE),
                usage=usage,
            )
        if result is not None:
            await _upsert_dimension(session, learner_id, spec, result)
    await session.commit()
    await _revise_lesson_plans(session, learner_id)
    return await get_snapshot(session, learner_id)


async def _revise_lesson_plans(session: AsyncSession, learner_id: uuid.UUID) -> None:
    """Refresh hints/pacing on every existing plan now that the profile changed — the
    "profile shifts" revision trigger (TECHNICAL_DESIGN §7.7).

    Lazy import: ``app.services.lesson_plan`` imports this module (to read the snapshot for
    scaffolding), so a top-level import here would be a real circular import. This is the
    deliberate back-edge.
    """
    from app.services import lesson_plan as lesson_plan_svc

    subject_ids = (
        await session.scalars(
            select(LessonPlan.subject_id).where(LessonPlan.learner_id == learner_id)
        )
    ).all()
    for subject_id in subject_ids:
        await lesson_plan_svc.revise_plan(session, learner_id=learner_id, subject_id=subject_id)


async def get_snapshot(session: AsyncSession, learner_id: uuid.UUID) -> list[ProfileDimension]:
    """The learner's current dimension rows, read-only (no recompute)."""
    return list(
        (
            await session.scalars(
                select(ProfileDimension)
                .where(ProfileDimension.learner_id == learner_id)
                .order_by(ProfileDimension.key)
            )
        ).all()
    )


async def reset_dimension(session: AsyncSession, learner_id: uuid.UUID, key: str) -> bool:
    """Delete a learner's stored value for ``key`` so it's recomputed fresh next refresh.

    Idempotent (no-op if the learner has no row for it). Raises ``KeyError`` if ``key`` isn't
    a dimension the catalog knows about at all — the router maps that to a 404.
    """
    if key not in {spec.key for spec in DIMENSION_SPECS}:
        raise KeyError(key)
    dim = await session.scalar(
        select(ProfileDimension).where(
            ProfileDimension.learner_id == learner_id, ProfileDimension.key == key
        )
    )
    if dim is None:
        return False
    await session.delete(dim)
    await session.commit()
    return True
