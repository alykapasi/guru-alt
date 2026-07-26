"""Knowledge-graph CRUD endpoints.

Every route requires an authenticated learner (the stub seam) even though the graph is
global content — this establishes the auth threading from day one.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentLearner, SessionDep
from app.schemas.knowledge import (
    KCCreate,
    KCDetail,
    KCRead,
    PrerequisiteCreate,
    PrerequisiteRead,
    SubjectCreate,
    SubjectRead,
    TopicCreate,
    TopicRead,
)
from app.services import knowledge as svc

router = APIRouter(tags=["knowledge"])


class SubjectCommitRequest(BaseModel):
    """Request to commit a subject with its topics and KCs."""

    subject_name: str
    subject_description: str | None = None
    topics: list[dict]
    source_ids: list[uuid.UUID] | None = None


@asynccontextmanager
async def _conflict_409(session: SessionDep) -> AsyncIterator[None]:
    """Translate constraint violations into a 409 (and roll the session back)."""
    try:
        yield
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="resource already exists"
        ) from exc


# --- Subjects ---------------------------------------------------------------


@router.post("/subjects", response_model=SubjectRead, status_code=status.HTTP_201_CREATED)
async def create_subject(data: SubjectCreate, session: SessionDep, _: CurrentLearner):
    async with _conflict_409(session):
        return await svc.create_subject(session, data)


@router.post("/subjects/commit", response_model=SubjectRead, status_code=status.HTTP_201_CREATED)
async def commit_subject(
    request: SubjectCommitRequest, session: SessionDep, learner: CurrentLearner
):
    """Commit a subject with its full topic/KC graph in one atomic transaction.

    Returns 409 if a subject with this name already exists (case-insensitive).
    """
    if await svc.subject_name_exists(session, request.subject_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="subject with this name already exists",
        )

    async with _conflict_409(session):
        return await svc.create_subject_with_graph(
            session,
            subject_name=request.subject_name,
            subject_description=request.subject_description,
            topics_data=request.topics,
            source_ids=request.source_ids,
            learner_id=learner.id,
        )


@router.get("/subjects", response_model=list[SubjectRead])
async def list_subjects(session: SessionDep, _: CurrentLearner):
    return await svc.list_subjects(session)


@router.get("/subjects/{subject_id}", response_model=SubjectRead)
async def get_subject(subject_id: uuid.UUID, session: SessionDep, _: CurrentLearner):
    subject = await svc.get_subject(session, subject_id)
    if subject is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    return subject


# --- Topics -----------------------------------------------------------------


@router.post(
    "/subjects/{subject_id}/topics",
    response_model=TopicRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_topic(
    subject_id: uuid.UUID, data: TopicCreate, session: SessionDep, _: CurrentLearner
):
    if await svc.get_subject(session, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    async with _conflict_409(session):
        return await svc.create_topic(session, subject_id, data)


@router.get("/subjects/{subject_id}/topics", response_model=list[TopicRead])
async def list_topics(subject_id: uuid.UUID, session: SessionDep, _: CurrentLearner):
    if await svc.get_subject(session, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    return await svc.list_topics(session, subject_id)


# --- KCs --------------------------------------------------------------------


@router.post("/topics/{topic_id}/kcs", response_model=KCRead, status_code=status.HTTP_201_CREATED)
async def create_kc(topic_id: uuid.UUID, data: KCCreate, session: SessionDep, _: CurrentLearner):
    if await svc.get_topic(session, topic_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "topic not found")
    async with _conflict_409(session):
        return await svc.create_kc(session, topic_id, data)


@router.get("/topics/{topic_id}/kcs", response_model=list[KCRead])
async def list_kcs(topic_id: uuid.UUID, session: SessionDep, _: CurrentLearner):
    if await svc.get_topic(session, topic_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "topic not found")
    return await svc.list_kcs(session, topic_id)


@router.get("/kcs/{kc_id}", response_model=KCDetail)
async def get_kc(kc_id: uuid.UUID, session: SessionDep, _: CurrentLearner):
    kc = await svc.get_kc(session, kc_id)
    if kc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "kc not found")
    edges = await svc.list_prerequisites(session, kc_id)
    return KCDetail(
        id=kc.id,
        topic_id=kc.topic_id,
        slug=kc.slug,
        name=kc.name,
        description=kc.description,
        prerequisites=[PrerequisiteRead.model_validate(e) for e in edges],
    )


# --- Prerequisite edges -----------------------------------------------------


@router.post(
    "/kcs/{kc_id}/prerequisites",
    response_model=PrerequisiteRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_prerequisite(
    kc_id: uuid.UUID,
    data: PrerequisiteCreate,
    session: SessionDep,
    _: CurrentLearner,
):
    if kc_id == data.prereq_kc_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a KC cannot be its own prerequisite")
    if await svc.get_kc(session, kc_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "kc not found")
    if await svc.get_kc(session, data.prereq_kc_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "prerequisite kc not found")
    async with _conflict_409(session):
        return await svc.add_prerequisite(session, kc_id, data.prereq_kc_id, data.weight)
