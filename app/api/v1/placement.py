"""The placement diagnostic: a fixed prompt, then one synchronous ask+infer+light-test call."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.core.config import get_settings
from app.schemas.assessment import KCEstimateRead
from app.schemas.placement import PlacementPromptRead, PlacementResultRead, PlacementSubmit
from app.services import assessment as assessment_svc
from app.services import knowledge as knowledge_svc
from app.services import placement as svc

router = APIRouter(tags=["placement"])


@router.get("/subjects/{subject_id}/placement/prompt", response_model=PlacementPromptRead)
async def get_placement_prompt(subject_id: uuid.UUID, session: SessionDep, _: CurrentLearner):
    subject = await knowledge_svc.get_subject(session, subject_id)
    if subject is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    return PlacementPromptRead(question=svc.BACKGROUND_QUESTION.format(subject=subject.name))


@router.post("/subjects/{subject_id}/placement", response_model=PlacementResultRead)
async def run_placement(
    subject_id: uuid.UUID,
    data: PlacementSubmit,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    subject = await knowledge_svc.get_subject(session, subject_id)
    if subject is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    result = await svc.run_placement(
        session,
        llm,
        learner_id=learner.id,
        subject=subject,
        background=data.background,
        light_test_size=get_settings().placement_light_test_size,
    )
    return PlacementResultRead(
        seeded=[KCEstimateRead.model_validate(s) for s in result.seeded],
        light_test_items=[assessment_svc.item_to_read(i) for i in result.light_test_items],
    )
