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

from app.api.deps import ConceptLinkJudgeEnqueuerDep, CurrentLearner, RetagEnqueuerDep, SessionDep
from app.models.knowledge import Subject
from app.models.publication import CurriculumProposal
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
    # Required, not optional (S25b D4). It names the server's own record of the generation this
    # graph came from, which is where `private_source_derived` is read from — and an *optional*
    # id is one a client can leave out, which is the same hole the record was built to close.
    proposal_id: uuid.UUID


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


async def _visible_subject(session, subject_id: uuid.UUID, learner) -> Subject:
    """The subject, if this learner may see it at all (S25).

    A subject owned by somebody else answers 404 rather than 403, and the difference is the
    point: 403 would confirm that a subject with that id exists, which is exactly what a
    learner must not be able to learn about another learner's private curriculum. Absent and
    not-yours are deliberately indistinguishable.
    """
    return await svc.require_visible_subject(session, subject_id, learner.id)


def _require_writable(subject: Subject, learner) -> None:
    """Raise unless this learner may change ``subject``'s graph.

    **Only ever called on a subject the caller can already see**, which is what leaves exactly
    one failing case here: a curated subject, which openly exists and is read-only through this
    API. Saying so plainly is useful and gives nothing away.

    Another learner's subject never reaches this — visibility rejected it as a 404 first, and
    that ordering is the design rather than an accident. An earlier version also handled it
    here, as a 404 for symmetry, and mutation testing showed the branch was unreachable: dead
    authorisation code, which is worse than none, because it reads as a protection that is
    never exercised and nothing would notice if it stopped working.
    """
    if not svc.is_writable_by(subject, learner.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "curated subjects are read-only; create your own subject to change its graph",
        )


async def _writable_subject_of_topic(session, topic_id: uuid.UUID, learner) -> Subject:
    subject, _ = await svc.require_visible_topic(session, topic_id, learner.id)
    _require_writable(subject, learner)
    return subject


async def _writable_subject_of_kc(session, kc_id: uuid.UUID, learner, *, missing: str) -> Subject:
    # ``missing`` survives because the two ends of an edge answer differently: "kc not found"
    # for the dependent, "prerequisite kc not found" for the prerequisite.
    try:
        subject, _ = await svc.require_visible_kc(session, kc_id, learner.id)
    except svc.NotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing) from exc
    _require_writable(subject, learner)
    return subject


# --- Subjects ---------------------------------------------------------------


@router.post("/subjects", response_model=SubjectRead, status_code=status.HTTP_201_CREATED)
async def create_subject(data: SubjectCreate, session: SessionDep, learner: CurrentLearner):
    """A subject created through this route belongs to its creator (S25). Curated subjects
    carry no owner and are not created here — nothing a learner can reach makes one."""
    async with _conflict_409(session):
        return await svc.create_subject(session, data, owner_learner_id=learner.id)


@router.post("/subjects/commit", response_model=SubjectRead, status_code=status.HTTP_201_CREATED)
async def commit_subject(
    request: SubjectCommitRequest,
    session: SessionDep,
    learner: CurrentLearner,
    retag: RetagEnqueuerDep,
    judge: ConceptLinkJudgeEnqueuerDep,
):
    """Commit a subject with its full topic/KC graph in one atomic transaction.

    Returns 409 if a subject with this name already exists (case-insensitive).
    """
    record = await session.get(CurriculumProposal, request.proposal_id)
    if record is None or record.learner_id != learner.id:
        # One answer for "no such proposal" and "somebody else's", as everywhere else on this
        # boundary: a caller able to tell them apart could probe for other learners' activity.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such proposal")

    if await svc.subject_name_exists(session, request.subject_name, owner_learner_id=learner.id):
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
            # From what the server observed, never from the body: the row remembers whether
            # the generation read this learner's uploads, and moving sources in is grounding
            # on its own.
            private_source_derived=record.grounded_in_sources or bool(request.source_ids),
        )
    # Moving a source between subjects invalidated its chunk KC tags, which the call above
    # already deleted. Rebuilding them is a model call per chunk, so it happens in the
    # background rather than holding this request open; until it lands the source simply has
    # no tags, which is the honest state, not a wrong one.
    for source_id in result.reassigned_source_ids:
        await ingestion_svc.dispatch(retag, source_id)
    # A new subject can share concepts with the learner's others and the library. Judging a
    # pair is a model call each, so it runs in the background; until it lands there are simply
    # no suggestions yet (S24).
    await judge(learner.id)
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
    await _visible_subject(session, subject_id, learner)
    return await svc.sacrificed_prerequisites(session, subject_id)


class ForeignPrerequisiteRead(BaseModel):
    """A prerequisite this subject declares on a component another subject owns (S24)."""

    prereq_kc_id: uuid.UUID
    prereq_name: str
    prereq_subject_id: uuid.UUID
    prereq_subject_name: str
    kc_id: uuid.UUID
    kc_name: str
    met_elsewhere: bool


@router.get(
    "/subjects/{subject_id}/cross-subject-prerequisites",
    response_model=list[ForeignPrerequisiteRead],
)
async def subject_cross_subject_prerequisites(
    subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner
):
    """Prerequisites of this subject that live in another subject (S24).

    A lesson plan sequences one subject's components, so a prerequisite outside it has no step
    that could teach it and planning drops the edge. That is a real weakening of the ordering
    and it used to be invisible — worse than invisible, since carrying the foreign component
    into the sort raised `TypeError` and no plan was produced at all.

    `met_elsewhere` says whether this learner has any presentation of that concept in their
    history. Deliberately not "has mastered it": two components share a concept on the evidence
    of their names, and treating that as transferred mastery would stop the product teaching
    something the learner has never seen.

    An empty list is the ordinary answer.
    """
    await _visible_subject(session, subject_id, learner)
    return await svc.cross_subject_prerequisites(session, subject_id, learner_id=learner.id)


@router.delete("/kcs/{kc_id}/prerequisites/{prereq_kc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_prerequisite(
    kc_id: uuid.UUID,
    prereq_kc_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
):
    """Remove one prerequisite edge — the repair path for a graph a plan could not honour.

    The conflict endpoint above reports which edges a plan had to sacrifice and deliberately
    changes nothing, because a cycle means two components each claim to come first and which
    claim is wrong is not something the graph knows. This is how a person acts on that report.

    404 only when the edge does not exist *and* neither does the KC — deleting an edge that is
    already gone succeeds, so a retried request behaves like the one that got through. Only the
    subject's owner may do it: editing the graph is editing the curriculum (S25).
    """
    await _writable_subject_of_kc(session, kc_id, learner, missing="kc not found")
    await svc.remove_prerequisite(session, kc_id=kc_id, prereq_kc_id=prereq_kc_id)


@router.get("/subjects/{subject_id}/coverage", response_model=list[KCCoverageRead])
async def subject_coverage(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Which KCs in this subject the learner's own library actually covers.

    A zero here means the KC can only be taught from the model's own knowledge, with no
    citable passage behind it — which is the more actionable half of the answer.
    """
    await _visible_subject(session, subject_id, learner)
    return await svc.kc_coverage(session, learner_id=learner.id, subject_id=subject_id)


@router.get("/subjects", response_model=list[SubjectRead])
async def list_subjects(session: SessionDep, learner: CurrentLearner):
    """Curated subjects and this learner's own — not everybody's (S25)."""
    return await svc.list_subjects(session, learner_id=learner.id)


@router.get("/subjects/{subject_id}", response_model=SubjectRead)
async def get_subject(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    return await _visible_subject(session, subject_id, learner)


# --- Topics -----------------------------------------------------------------


@router.post(
    "/subjects/{subject_id}/topics",
    response_model=TopicRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_topic(
    subject_id: uuid.UUID, data: TopicCreate, session: SessionDep, learner: CurrentLearner
):
    _require_writable(await _visible_subject(session, subject_id, learner), learner)
    async with _conflict_409(session):
        return await svc.create_topic(session, subject_id, data)


@router.get("/subjects/{subject_id}/topics", response_model=list[TopicRead])
async def list_topics(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    await _visible_subject(session, subject_id, learner)
    return await svc.list_topics(session, subject_id)


# --- KCs --------------------------------------------------------------------


@router.post("/topics/{topic_id}/kcs", response_model=KCRead, status_code=status.HTTP_201_CREATED)
async def create_kc(
    topic_id: uuid.UUID, data: KCCreate, session: SessionDep, learner: CurrentLearner
):
    await _writable_subject_of_topic(session, topic_id, learner)
    async with _conflict_409(session):
        return await svc.create_kc(session, topic_id, data)


@router.get("/topics/{topic_id}/kcs", response_model=list[KCRead])
async def list_kcs(topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    await svc.require_visible_topic(session, topic_id, learner.id)
    return await svc.list_kcs(session, topic_id)


@router.get("/kcs/{kc_id}", response_model=KCDetail)
async def get_kc(kc_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    _, kc = await svc.require_visible_kc(session, kc_id, learner.id)
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
    learner: CurrentLearner,
):
    if kc_id == data.prereq_kc_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a KC cannot be its own prerequisite")
    # Both ends are checked, not just the dependent. An edge constrains the order both
    # components are taught in, so writing one into a subject the caller does not own is a
    # change to somebody else's curriculum however this end is addressed (S25). The
    # prerequisite may legitimately live in another of the caller's subjects — that is S24's
    # cross-subject edge, and it is theirs to create.
    await _writable_subject_of_kc(session, kc_id, learner, missing="kc not found")
    await _writable_subject_of_kc(
        session, data.prereq_kc_id, learner, missing="prerequisite kc not found"
    )
    # 409 rather than the 400 a self-prerequisite gets, and the difference is real: a
    # self-loop is wrong in isolation, while this edge is only wrong against the graph that
    # happens to be stored. The check lives in the service, under the edge lock, so two
    # requests cannot each pass it against a graph missing the other's edge (S23).
    try:
        async with _conflict_409(session):
            return await svc.add_prerequisite(session, kc_id, data.prereq_kc_id, data.weight)
    except svc.WouldCreateCycle as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "that prerequisite would create a cycle: the proposed prerequisite already "
            "depends on this knowledge component",
        ) from exc
