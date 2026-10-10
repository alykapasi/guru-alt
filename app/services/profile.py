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
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning.profile_estimators import (
    DIMENSION_SPECS,
    DimensionEstimate,
    DimensionSpec,
    EstimatorContext,
)
from app.llm import LLMClient
from app.llm.attribution import metered
from app.llm.meter import CallRefused
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.profile import LearnerProfile, ProfileDimension


async def _load_events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    """The learner's most recent events, oldest first (S43: a window, not the whole history)."""
    rows = (
        await session.scalars(
            select(LearningEvent)
            .where(LearningEvent.learner_id == learner_id)
            .order_by(LearningEvent.created_at.desc(), LearningEvent.id.desc())
            .limit(get_settings().profile_event_window)
        )
    ).all()
    return list(reversed(rows))


def readable_own_messages() -> tuple[ColumnElement[bool], ...]:
    """What the profile may read of a learner's messages; needs ``Conversation`` and
    ``Learner`` joined.

    Their own messages, never an administrator's (S43). None while memory is paused, and none
    written before it was last resumed (O07): pausing "Remember things from my conversations"
    stops the profile reading what they type, and resuming is not consent to read the pause.
    """
    return (
        Message.role == "user",
        Message.admin_actor_id.is_(None),
        Message.admin_action_id.is_(None),
        Learner.remember_conversations.is_(True),
        or_(
            Learner.profile_messages_since.is_(None),
            Message.created_at > Learner.profile_messages_since,
        ),
    )


async def _load_own_messages(session: AsyncSession, learner_id: uuid.UUID) -> list[Message]:
    """The learner's most recent readable messages, oldest first (S43: a window)."""
    rows = (
        await session.scalars(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Learner, Learner.id == Conversation.learner_id)
            .where(Conversation.learner_id == learner_id, *readable_own_messages())
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(get_settings().profile_message_window)
        )
    ).all()
    return list(reversed(rows))


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
    fingerprint: str | None,
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
                input_fingerprint=fingerprint,
            )
        )
        return
    dim.value = result.value
    dim.uncertainty = result.uncertainty
    dim.kind = spec.kind
    dim.source = spec.source
    dim.input_fingerprint = fingerprint


async def latest_evidence_at(session: AsyncSession, learner_id: uuid.UUID) -> datetime | None:
    """When this learner last produced anything a dimension is estimated from.

    Two cheap MAX() reads standing in for loading the whole history to discover it has not
    changed — which is what a refresh over unchanged evidence was doing, several model calls
    at a time.

    Graded observations only, because that is the evidence the estimators actually read:
    ``profile_estimators._observations`` drops every self-rated row, and no estimator reads
    the raw stream. A cursor that moved on a ``self_report`` would therefore claim new
    evidence for a recompute that provably cannot produce a different answer — a full pass
    over DIMENSION_SPECS, model-backed classifier included, plus ``_revise_lesson_plans``,
    per review batch (S56). Self-rating asks for a review, not for a re-read of the learner.
    Admin-visit messages are excluded like everywhere else the learner's evidence is read (S43),
    and so is anything the profile may not read while memory is paused (O07).
    """
    newest_event = await session.scalar(
        select(func.max(LearningEvent.created_at)).where(
            LearningEvent.learner_id == learner_id,
            LearningEvent.event_type == "observation",
        )
    )
    newest_message = await session.scalar(
        select(func.max(Message.created_at))
        .join(Conversation, Message.conversation_id == Conversation.id)
        .join(Learner, Learner.id == Conversation.learner_id)
        .where(Conversation.learner_id == learner_id, *readable_own_messages())
    )
    stamps = [s for s in (newest_event, newest_message) if s is not None]
    return max(stamps) if stamps else None


@metered("profile_refresh", learner="learner_id")
async def refresh_profile(
    session: AsyncSession, learner_id: uuid.UUID, llm: LLMClient, *, force: bool = False
) -> list[ProfileDimension]:
    """Recompute every dimension in the catalog from the learner's current history.

    Estimators that return ``None`` (not enough evidence yet) leave that dimension untouched —
    a thin history means fewer dimensions computed, never a fabricated confident value.

    Skipped entirely when no new evidence has arrived since the last run (S43): every
    dimension is recomputed from scratch each time, so repeating that over an unchanged
    history costs several model calls to produce the values already stored. ``force`` runs it
    anyway, which is what you want after the estimators themselves change — the cursor tracks
    the *evidence*, and cannot know the code that reads it moved.

    The recompute reads a recency window (``profile_event_window`` events and
    ``profile_message_window`` messages), so its cost does not grow with the history, and a
    model-backed dimension whose input fingerprint is unchanged keeps its value without a call
    (S43). ``force`` ignores fingerprints too.
    """
    profile = await _ensure_profile(session, learner_id)
    newest = await latest_evidence_at(session, learner_id)
    if not force and newest is not None and profile.evidence_watermark == newest:
        return await get_snapshot(session, learner_id)

    try:
        context = EstimatorContext(
            session=session,
            learner_id=learner_id,
            events=await _load_events(session, learner_id),
            messages=await _load_own_messages(session, learner_id),
            llm=llm,
        )
        stored = {
            key: fingerprint
            for key, fingerprint in (
                await session.execute(
                    select(ProfileDimension.key, ProfileDimension.input_fingerprint).where(
                        ProfileDimension.learner_id == learner_id
                    )
                )
            ).all()
        }
        paused = not await session.scalar(
            select(Learner.remember_conversations).where(Learner.id == learner_id)
        )
        for spec in DIMENSION_SPECS:
            if paused and spec.reads_messages:
                continue  # O07: the last value stands, shown as paused
            fingerprint = await spec.fingerprint(context) if spec.fingerprint else None
            if not force and fingerprint is not None and stored.get(spec.key) == fingerprint:
                continue  # the same input as last time: the stored answer stands, unpaid
            result, _usage = await spec.estimate(context)
            if result is not None:
                await _upsert_dimension(session, learner_id, spec, result, fingerprint)
    except CallRefused:
        # Refused, not broken (S47): nothing to record against the profile. Retried when the
        # spend window allows, since the watermark has not moved.
        await session.rollback()
        raise
    except Exception as exc:
        # Record why, then re-raise. A profile that quietly stopped updating is
        # indistinguishable from one nothing has changed for, and the watermark deliberately
        # does not advance — a failed run must not mark this evidence as processed.
        # Note the rollback expires every ORM object the caller's session holds. That is
        # correct here (a half-applied set of dimension upserts must not survive) and safe in
        # the request path, where this is the only thing using the session.
        await session.rollback()
        profile = await _ensure_profile(session, learner_id)
        profile.last_error = str(exc)[:1000]
        await session.commit()
        raise

    profile.evidence_watermark = newest
    profile.refreshed_at = datetime.now(UTC).replace(tzinfo=None)
    profile.last_error = None
    profile.refresh_attempted_at = None
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
    # The evidence cursor says "these dimensions already reflect this history". A reset makes
    # that false without touching the evidence, so the cursor has to be cleared or the next
    # refresh would skip the very recomputation the reset asked for.
    profile = await _ensure_profile(session, learner_id)
    profile.evidence_watermark = None
    await session.commit()
    return True
