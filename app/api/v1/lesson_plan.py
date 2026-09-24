"""The lesson plan: generate/regenerate, view, close or reopen its goal, and — S11 — let the
learner set how much say they have over a prerequisite detour and decide on one that is open."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.learning import lesson_plan as engine
from app.schemas.lesson_plan import (
    DetourDecisionSubmit,
    LessonPlanClosureSubmit,
    LessonPlanGuidanceSubmit,
    LessonPlanRead,
    LessonPlanSubmit,
)
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
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    plan = await svc.generate_lesson_plan(
        session, llm, learner_id=learner.id, subject_id=subject_id, goal=data.goal
    )
    return await svc.plan_read(session, plan)


@router.get("/subjects/{subject_id}/lesson-plan", response_model=LessonPlanRead)
async def get_lesson_plan(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    plan = await svc.get_lesson_plan(session, learner.id, subject_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lesson plan for this subject yet")
    return await svc.plan_read(session, plan)


@router.patch("/subjects/{subject_id}/lesson-plan/closure", response_model=LessonPlanRead)
async def set_lesson_plan_closure(
    subject_id: uuid.UUID,
    data: LessonPlanClosureSubmit,
    session: SessionDep,
    learner: CurrentLearner,
):
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    plan = await svc.set_goal_closed(
        session, learner_id=learner.id, subject_id=subject_id, closed=data.closed
    )
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lesson plan for this subject yet")
    return await svc.plan_read(session, plan)


@router.patch("/subjects/{subject_id}/lesson-plan/guidance", response_model=LessonPlanRead)
async def set_lesson_plan_guidance(
    subject_id: uuid.UUID,
    data: LessonPlanGuidanceSubmit,
    session: SessionDep,
    learner: CurrentLearner,
):
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    plan = await svc.set_guidance(
        session, learner_id=learner.id, subject_id=subject_id, guidance=data.guidance
    )
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lesson plan for this subject yet")
    return await svc.plan_read(session, plan)


@router.post(
    "/subjects/{subject_id}/lesson-plan/detours/{prereq_kc_id}", response_model=LessonPlanRead
)
async def decide_lesson_plan_detour(
    subject_id: uuid.UUID,
    prereq_kc_id: uuid.UUID,
    data: DetourDecisionSubmit,
    session: SessionDep,
    learner: CurrentLearner,
):
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    try:
        plan = await svc.decide_detour(
            session,
            learner_id=learner.id,
            subject_id=subject_id,
            prereq_kc_id=prereq_kc_id,
            decision=data.decision,
        )
    except engine.DetourNotOpen as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that detour is not open for a decision"
        ) from exc
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lesson plan for this subject yet")
    return await svc.plan_read(session, plan)
