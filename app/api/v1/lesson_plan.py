"""The lesson plan: generate/regenerate and view."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.schemas.lesson_plan import LessonPlanRead, LessonPlanSubmit
from app.services import knowledge as knowledge_svc
from app.services import lesson_plan as svc

router = APIRouter(tags=["lesson-plan"])


@router.post("/subjects/{subject_id}/lesson-plan", response_model=LessonPlanRead)
async def generate_lesson_plan(
    subject_id: uuid.UUID,
    data: LessonPlanSubmit,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    if await knowledge_svc.get_subject(session, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    return await svc.generate_lesson_plan(
        session, llm, learner_id=learner.id, subject_id=subject_id, goal=data.goal
    )


@router.get("/subjects/{subject_id}/lesson-plan", response_model=LessonPlanRead)
async def get_lesson_plan(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    if await knowledge_svc.get_subject(session, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    plan = await svc.get_lesson_plan(session, learner.id, subject_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lesson plan for this subject yet")
    return plan
