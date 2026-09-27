"""A learner's explicit settings (S02, V09): read with provenance, set per level."""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentLearner, SessionDep
from app.learning import lesson_plan as plan_engine
from app.learning.preferences import CATALOG, HINT_DENSITY
from app.schemas.preference import PreferenceRead, PreferenceSubmit
from app.services import knowledge as knowledge_svc
from app.services import preferences as svc
from app.services import profile as profile_svc

router = APIRouter(tags=["preferences"])

_HINT_WORD = {density: word for word, density in HINT_DENSITY.items()}


async def _inferred(session: AsyncSession, learner_id: uuid.UUID) -> dict[str, str | None]:
    """What inference would choose for the keys it covers — shown, never used while pinned."""
    snapshot = await profile_svc.get_snapshot(session, learner_id)
    values = {d.key: d.value for d in snapshot}
    hints = plan_engine.scaffolding_from_profile(values)
    note_format = values.get("note_format")
    return {
        "hints": _HINT_WORD.get(hints.hint_density or ""),
        # ScaffoldingHints.pacing defaults to "standard" even with no evidence, so only report
        # a pace when the dimension exists.
        "pace": hints.pacing if isinstance(values.get("pace"), dict) else None,
        "note_format": note_format
        if isinstance(note_format, str) and note_format in CATALOG["note_format"].options
        else None,
    }


async def _read(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID | None
) -> list[PreferenceRead]:
    resolved = await svc.effective(session, learner_id, subject_id)
    global_level = (
        await svc.effective(session, learner_id, None) if subject_id is not None else None
    )
    inferred = await _inferred(session, learner_id)
    return [
        PreferenceRead(
            key=key,
            label=spec.label,
            value=resolved[key].value,
            source=resolved[key].source,
            options=list(spec.options),
            global_value=global_level[key].value if global_level is not None else None,
            inferred=inferred.get(key),
        )
        for key, spec in CATALOG.items()
    ]


@router.get("/preferences", response_model=list[PreferenceRead])
async def list_preferences(
    session: SessionDep, learner: CurrentLearner, subject_id: uuid.UUID | None = None
):
    """Every setting, what it resolves to here, and where that came from."""
    if subject_id is not None:
        await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    return await _read(session, learner.id, subject_id)


@router.put("/preferences/{key}", response_model=PreferenceRead)
async def set_preference(
    key: str, data: PreferenceSubmit, session: SessionDep, learner: CurrentLearner
):
    """Pin a setting globally or for one subject; null clears that level."""
    if data.subject_id is not None:
        await knowledge_svc.require_visible_subject(session, data.subject_id, learner.id)
    learner_id = learner.id
    try:
        await svc.set_preference(session, learner_id, key, data.value, subject_id=data.subject_id)
    except svc.InvalidPreference as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_preference", "message": f"Not a setting: {exc}"},
        ) from exc
    [entry] = [p for p in await _read(session, learner_id, data.subject_id) if p.key == key]
    return entry
