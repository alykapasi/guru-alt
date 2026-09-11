"""Assessment endpoints: author items and answer them through the adaptive loop."""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.core.config import get_settings
from app.learning.grading import InvalidResponse, SelfGradeError
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
from app.services import session_runner as session_runner_svc

GRADABLE = AUTO_GRADABLE | RUBRIC_GRADABLE | SELF_GRADABLE
"""Every item type the answer loop can grade: objective, open (rubric), and self-rated."""

router = APIRouter(tags=["assessment"])


@router.post("/items", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
async def create_item(data: ItemCreate, session: SessionDep, learner: CurrentLearner):
    """Author an item. It is this learner's alone — being signed in is not authority to write
    a question, and an answer key, that other learners are then examined against (S33)."""
    try:
        item = await svc.create_item(session, data, author_learner_id=learner.id)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "unknown kc_id or rubric_id, or duplicate KC"
        ) from exc
    return svc.item_to_read(item)


@router.get("/items/{item_id}", response_model=ItemRead)
async def get_item(item_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    item = await svc.get_item_for(session, item_id, learner_id=learner.id)
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
    # Scoped like the read: answering another learner's private item would write a mastery
    # observation from a question nobody vouched for (S33).
    item = await svc.get_item_for(session, item_id, learner_id=learner.id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    if ItemType(item.item_type) not in GRADABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{item.item_type} items need self-grading (not yet available)",
        )
    try:
        result, states = await svc.answer_item(session, learner.id, item, submission, llm=llm)
    except (InvalidResponse, SelfGradeError) as exc:
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
        component_scores=result.component_scores,
    )


@router.get("/reviews/due", response_model=list[ReviewItemRead])
async def due_reviews(session: SessionDep, learner: CurrentLearner, llm: LLMClientDep):
    """KCs whose FSRS-scheduled review has come due, soonest first, each paired with an
    answerable flashcard where one was eagerly resolved (see ``session_runner.due_review_items``
    and the ``reviews_due_item_limit`` cost bound)."""
    pairs = await session_runner_svc.due_review_items(
        session, llm, learner_id=learner.id, item_limit=get_settings().reviews_due_item_limit
    )
    return [
        ReviewItemRead(
            kc_id=review.kc_id,
            due_at=review.due_at,
            ability=review.ability,
            uncertainty=review.uncertainty,
            item=svc.item_to_read(item) if item is not None else None,
        )
        for review, item in pairs
    ]
