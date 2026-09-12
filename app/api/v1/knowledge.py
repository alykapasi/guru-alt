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

from app.api.deps import CurrentLearner, RetagEnqueuerDep, SessionDep
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
from app.services import ingestion as ingestion_svc
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
    request: SubjectCommitRequest,
    session: SessionDep,
    learner: CurrentLearner,
    retag: RetagEnqueuerDep,
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
        result = await svc.create_subject_with_graph(
            session,
            subject_name=request.subject_name,
            subject_description=request.subject_description,
            topics_data=request.topics,
            source_ids=request.source_ids,
            learner_id=learner.id,
        )
    # Moving a source between subjects invalidated its chunk KC tags, which the call above
    # already deleted. Rebuilding them is a model call per chunk, so it happens in the
    # background rather than holding this request open; until it lands the source simply has
    # no tags, which is the honest state, not a wrong one.
    for source_id in result.reassigned_source_ids:
        await ingestion_svc.dispatch(retag, source_id)
    return result.subject


class KCCoverageRead(BaseModel):
    kc_id: uuid.UUID
    slug: str
    name: str
    chunk_count: int


class SacrificedEdgeRead(BaseModel):
    prereq_kc_id: uuid.UUID
    prereq_slug: str
    prereq_name: str
    kc_id: uuid.UUID
    kc_slug: str
    kc_name: str


@router.get(
    "/subjects/{subject_id}/prerequisite-conflicts",
    response_model=list[SacrificedEdgeRead],
)
async def subject_prerequisite_conflicts(
    subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner
):
    """Prerequisites this subject declares that its lesson plans cannot honour (S23).

    A cycle means two components each claim to come before the other. Planning resolves it by
    dropping whichever edge closes the ring, so a plan is still produced — but one component
    is then scheduled before something it was declared to depend on, and until now that showed
    up only in a log line. An empty list is the ordinary answer and means the stored graph
    justifies the order the learner is taught in.
    """
    if await svc.get_subject(session, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    return await svc.sacrificed_prerequisites(session, subject_id)


@router.get("/subjects/{subject_id}/coverage", response_model=list[KCCoverageRead])
async def subject_coverage(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Which KCs in this subject the learner's own library actually covers.

    A zero here means the KC can only be taught from the model's own knowledge, with no
    citable passage behind it — which is the more actionable half of the answer.
    """
    if await svc.get_subject(session, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    return await svc.kc_coverage(session, learner_id=learner.id, subject_id=subject_id)


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
    # 409 rather than the 400 a self-prerequisite gets, and the difference is real: a
    # self-loop is wrong in isolation, while this edge is only wrong against the graph that
    # happens to be stored. Until this check existed a client could build any longer cycle
    # one valid-looking edge at a time, and nothing downstream would report it — plan
    # ordering just silently stopped being justified by the graph (S23).
    if await svc.would_create_cycle(session, kc_id=kc_id, prereq_kc_id=data.prereq_kc_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "that prerequisite would create a cycle: the proposed prerequisite already "
            "depends on this knowledge component",
        )
    async with _conflict_409(session):
        return await svc.add_prerequisite(session, kc_id, data.prereq_kc_id, data.weight)
