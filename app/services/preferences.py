"""Resolve a learner's explicit settings (S02, V09): subject override → global → default.

The one place any consumer asks — the tutor context, lesson generation, the note-format cascade
and lesson-plan guidance — so they cannot disagree about what the learner chose.
"""

import uuid
from dataclasses import dataclass
from typing import Literal, cast

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.preferences import CATALOG, is_valid
from app.models.preference import LearnerPreference

log = structlog.get_logger(__name__)

Source = Literal["subject", "global", "default"]


class InvalidPreference(ValueError):
    """A key not in the catalog, or a value not among that key's options."""


@dataclass(frozen=True)
class Resolved:
    value: str
    source: Source


def _defaults() -> dict[str, Resolved]:
    return {key: Resolved(spec.default, "default") for key, spec in CATALOG.items()}


async def effective(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID | None
) -> dict[str, Resolved]:
    """Every catalog key, resolved. One query, no model call.

    A stored row whose key or value is no longer in the catalog is skipped, never an error:
    the catalog is expected to change, and a stale row must not break a turn.
    """
    scope = LearnerPreference.subject_id.is_(None)
    if subject_id is not None:
        scope = or_(scope, LearnerPreference.subject_id == subject_id)
    rows = (
        await session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner_id, scope)
        )
    ).all()
    resolved = _defaults()
    # Global first, then subject, so the subject row overwrites.
    for row in sorted(rows, key=lambda r: r.subject_id is not None):
        if is_valid(row.key, row.value):
            resolved[row.key] = Resolved(row.value, "subject" if row.subject_id else "global")
    return resolved


async def values_for(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID | None
) -> dict[str, str]:
    """Just the values, for prompt assembly. Never raises: a failed read gives the defaults."""
    try:
        resolved = await effective(session, learner_id, subject_id)
    except Exception:
        log.exception("preferences.read_failed", learner_id=str(learner_id))
        resolved = _defaults()
    return {key: r.value for key, r in resolved.items()}


async def guidance_for(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> Literal["guided", "exploration"]:
    value = (await effective(session, learner_id, subject_id))["guidance"].value
    return cast("Literal['guided', 'exploration']", value)


async def set_preference(
    session: AsyncSession,
    learner_id: uuid.UUID,
    key: str,
    value: str | None,
    *,
    subject_id: uuid.UUID | None,
) -> None:
    """Record an explicit choice at one level, or clear that level with ``None``. Commits.

    A subject stores whatever it is given, the catalog default included: "guided here" or
    "adapt to me here" while the global setting says otherwise is a real choice, and an
    override equal to the global value stays pinned when the global value later moves. Only
    the global level treats the catalog default as "no row", since nothing sits below it.
    """
    at_level = (
        LearnerPreference.subject_id.is_(None)
        if subject_id is None
        else LearnerPreference.subject_id == subject_id
    )
    if value is not None and not is_valid(key, value):
        raise InvalidPreference(f"{key}={value}")
    if key not in CATALOG:
        raise InvalidPreference(key)
    if value is None or (subject_id is None and value == CATALOG[key].default):
        await session.execute(
            delete(LearnerPreference).where(
                LearnerPreference.learner_id == learner_id, LearnerPreference.key == key, at_level
            )
        )
    else:
        await session.execute(
            pg_insert(LearnerPreference)
            .values(
                id=uuid.uuid4(), learner_id=learner_id, subject_id=subject_id, key=key, value=value
            )
            .on_conflict_do_update(
                index_elements=["learner_id", "subject_id", "key"],
                set_={"value": value, "updated_at": func.now()},
            )
        )
    await session.commit()
