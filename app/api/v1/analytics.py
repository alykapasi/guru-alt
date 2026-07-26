"""The stats dashboard's read-only endpoints: mastery rollups + the activity summary."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, SessionDep
from app.schemas.analytics import ActivityRead, SubjectMasteryRead
from app.services import analytics as svc
from app.services import knowledge as knowledge_svc

router = APIRouter(tags=["analytics"])


@router.get("/subjects/{subject_id}/mastery", response_model=SubjectMasteryRead)
async def get_subject_mastery(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    if await knowledge_svc.get_subject(session, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    return await svc.subject_mastery(session, learner.id, subject_id)


@router.get("/activity", response_model=ActivityRead)
async def get_activity(session: SessionDep, learner: CurrentLearner):
    return await svc.get_activity(session, learner.id)
