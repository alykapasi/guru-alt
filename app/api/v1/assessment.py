"""Assessment endpoints: author items and answer them through the adaptive loop."""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.learning import mastery
from app.learning.grading import SelfGradeError
from app.learning.rubric_grading import RubricGradingError
from app.models.assessment import AUTO_GRADABLE, RUBRIC_GRADABLE, SELF_GRADABLE, ItemType
from app.schemas.assessment import (
    AnswerSubmit,
    GradeRead,
    ItemCreate,
    ItemRead,
    KCEstimateRead,
    ReviewItemRead,
)
from app.services import assessment as svc

GRADABLE = AUTO_GRADABLE | RUBRIC_GRADABLE | SELF_GRADABLE
"""Every item type the answer loop can grade: objective, open (rubric), and self-rated."""

router = APIRouter(tags=["assessment"])


@router.post("/items", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
async def create_item(data: ItemCreate, session: SessionDep, _: CurrentLearner):
    try:
        item = await svc.create_item(session, data)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "unknown kc_id or rubric_id, or duplicate KC"
        ) from exc
    return svc.item_to_read(item)


@router.get("/items/{item_id}", response_model=ItemRead)
async def get_item(item_id: uuid.UUID, session: SessionDep, _: CurrentLearner):
    item = await svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    return svc.item_to_read(item)


@router.post("/items/{item_id}/answer", response_model=GradeRead)
async def answer_item(
    item_id: uuid.UUID,
    submission: AnswerSubmit,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    item = await svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    if ItemType(item.item_type) not in GRADABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{item.item_type} items need self-grading (not yet available)",
        )
    try:
        result, states = await svc.answer_item(session, learner.id, item, submission, llm=llm)
    except SelfGradeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except RubricGradingError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "automatic grading failed; please retry"
        ) from exc
    return GradeRead(
        score=result.score,
        correct=result.correct,
        detail=result.detail,
        estimates=[KCEstimateRead.model_validate(s) for s in states],
    )


@router.get("/reviews/due", response_model=list[ReviewItemRead])
async def due_reviews(session: SessionDep, learner: CurrentLearner):
    """KCs whose FSRS-scheduled review has come due, soonest first."""
    items = await mastery.DEFAULT_TRACER.due_reviews(session, learner.id)
    return [ReviewItemRead.model_validate(i) for i in items]
