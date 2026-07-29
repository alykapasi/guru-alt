"""Notes endpoints: living per-topic study notes (Phase 8).

GET is pure; the work (distill/render/absorb — seconds of latency) sits behind explicit
POST/PUT/PATCH so reads never have generation side-effects.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.models.knowledge import Topic
from app.schemas.note import (
    NoteEditRequest,
    NoteFormatRequest,
    NoteIndexEntry,
    NoteRead,
    NoteRevisionRead,
    NoteRevisionSource,
)
from app.services import knowledge as knowledge_svc
from app.services import notes as notes_svc

router = APIRouter(tags=["notes"])


async def _topic_404(session: SessionDep, topic_id: uuid.UUID) -> Topic:
    topic = await knowledge_svc.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="topic not found")
    return topic


def _read(view: notes_svc.NoteView) -> NoteRead:
    return NoteRead(
        topic_id=view.topic_id,
        content_md=view.content_md,
        format=view.format,  # ty: ignore[invalid-argument-type]
        effective_format=view.effective_format,  # ty: ignore[invalid-argument-type]
        stale=view.stale,
        revision_ordinal=view.revision_ordinal,
        updated_at=view.updated_at,
    )


@router.get("/subjects/{subject_id}/notes", response_model=list[NoteIndexEntry])
async def notes_index(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    subject = await knowledge_svc.get_subject(session, subject_id)
    if subject is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="subject not found")
    return await notes_svc.notes_index(session, learner.id, subject_id)


@router.get("/topics/{topic_id}/note", response_model=NoteRead)
async def get_note(topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    topic = await _topic_404(session, topic_id)
    return _read(await notes_svc.note_view(session, learner.id, topic))


@router.post("/topics/{topic_id}/note/refresh", response_model=NoteRead)
async def refresh_note(
    topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner, llm: LLMClientDep
):
    topic = await _topic_404(session, topic_id)
    return _read(await notes_svc.refresh_note(session, llm, learner.id, topic))


@router.put("/topics/{topic_id}/note", response_model=NoteRead)
async def edit_note(
    topic_id: uuid.UUID,
    request: NoteEditRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    topic = await _topic_404(session, topic_id)
    if await notes_svc.get_note(session, learner.id, topic_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no note to edit yet")
    view = await notes_svc.absorb_edit(session, llm, learner.id, topic, request.content_md)
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Edit could not be absorbed. Your note is unchanged — please try again.",
        )
    return _read(view)


@router.patch("/topics/{topic_id}/note/format", response_model=NoteRead)
async def set_format(
    topic_id: uuid.UUID,
    request: NoteFormatRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    topic = await _topic_404(session, topic_id)
    return _read(await notes_svc.set_format(session, llm, learner.id, topic, request.format))


@router.get("/topics/{topic_id}/note/revisions", response_model=list[NoteRevisionRead])
async def list_revisions(topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    topic = await _topic_404(session, topic_id)
    return await notes_svc.list_revisions(session, learner.id, topic)


@router.get("/topics/{topic_id}/note/revisions/{ordinal}", response_model=NoteRevisionSource)
async def revision_source(
    topic_id: uuid.UUID, ordinal: int, session: SessionDep, learner: CurrentLearner
):
    topic = await _topic_404(session, topic_id)
    source = await notes_svc.revision_source(session, learner.id, topic, ordinal)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="revision not found")
    return NoteRevisionSource(ordinal=ordinal, content_md=source)


@router.post("/topics/{topic_id}/note/revisions/{ordinal}/restore", response_model=NoteRead)
async def restore_revision(
    topic_id: uuid.UUID,
    ordinal: int,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    topic = await _topic_404(session, topic_id)
    view = await notes_svc.restore_revision(session, llm, learner.id, topic, ordinal)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="revision not found")
    return _read(view)
