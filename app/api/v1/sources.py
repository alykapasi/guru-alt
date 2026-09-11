"""Ingestion endpoints: upload a source, then poll its status / inspect its chunks."""

import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select

from app.api.deps import (
    BlobStoreDep,
    CurrentLearner,
    IngestionEnqueuerDep,
    LLMClientDep,
    SessionDep,
    SettingsDep,
)
from app.models.source import Chunk, Source, SourceKind
from app.rag import retrieval
from app.rag.retrieval import RetrievalHit
from app.schemas.source import ChunkRead, LinkCreate, RetrieveRequest, SourceRead
from app.services import ingestion as svc
from app.services import knowledge

router = APIRouter(tags=["sources"])

_UPLOAD_CHUNK = 1024 * 1024  # 1 MiB — stream the upload to disk without buffering it in RAM


def _scoped(create):
    """Turn an inconsistent source scope into a 422 rather than a stored contradiction.

    A source scoped to a topic outside its subject is filtered out by *both* halves of
    retrieval, so it would be embedded, indexed, paid for, and unreachable (S55).
    """

    async def call(*args, **kwargs):
        try:
            return await create(*args, **kwargs)
        except knowledge.ScopeConflict as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    return call


@router.post("/sources/upload", response_model=SourceRead, status_code=status.HTTP_202_ACCEPTED)
async def upload_source(
    session: SessionDep,
    learner: CurrentLearner,
    blobstore: BlobStoreDep,
    enqueue: IngestionEnqueuerDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    subject_id: Annotated[uuid.UUID | None, Form()] = None,
    topic_id: Annotated[uuid.UUID | None, Form()] = None,
):
    """Stream an upload to a temp file (size-capped), store it, and queue ingestion.

    The file is streamed to disk in chunks so an arbitrarily large upload never sits in
    memory; it is rejected with 413 the moment it exceeds ``max_upload_bytes``.
    """
    tmp = tempfile.NamedTemporaryFile(dir=settings.ingest_tmp_dir, delete=False)
    tmp_path = Path(tmp.name)
    try:
        size = 0
        with tmp:
            while chunk := await file.read(_UPLOAD_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file too large")
                tmp.write(chunk)
        if size == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
        source = await _scoped(svc.create_source)(
            session,
            blobstore,
            learner_id=learner.id,
            kind=SourceKind.FILE,
            origin=file.filename or "upload",
            content_type=file.content_type,
            data=tmp_path,
            subject_id=subject_id,
            topic_id=topic_id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    await svc.dispatch(enqueue, source.id)
    return source


@router.post("/sources/link", response_model=SourceRead, status_code=status.HTTP_202_ACCEPTED)
async def link_source(
    data: LinkCreate,
    session: SessionDep,
    learner: CurrentLearner,
    enqueue: IngestionEnqueuerDep,
):
    """Register a web page for ingestion. The page is fetched in the background job."""
    source = await _scoped(svc.create_url_source)(
        session,
        learner_id=learner.id,
        url=str(data.url),
        subject_id=data.subject_id,
        topic_id=data.topic_id,
    )
    await svc.dispatch(enqueue, source.id)
    return source


@router.post(
    "/sources/{source_id}/retry",
    response_model=SourceRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_source(
    source_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
    enqueue: IngestionEnqueuerDep,
):
    """Re-run ingestion for a finished or failed source.

    A completed source is deliberately not claimable by a job (S37), so re-ingesting one has
    to be asked for. 409 while a claim is live rather than yanking work in flight.
    """
    source = await session.get(Source, source_id)
    if source is None or source.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    reset = await svc.reset_for_reingest(session, source_id)
    if reset is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this source is being ingested right now")
    await svc.dispatch(enqueue, reset.id)
    return reset


@router.get("/sources", response_model=list[SourceRead])
async def list_sources(
    session: SessionDep,
    learner: CurrentLearner,
    subject_id: Annotated[uuid.UUID | None, Query()] = None,
):
    """List the learner's sources, optionally scoped to a subject — backs the conversation
    creation modal's source picker (Phase 7)."""
    stmt = select(Source).where(Source.learner_id == learner.id)
    if subject_id is not None:
        stmt = stmt.where(Source.subject_id == subject_id)
    sources = (await session.scalars(stmt.order_by(Source.created_at.desc()))).all()
    return list(sources)


@router.get("/sources/{source_id}", response_model=SourceRead)
async def get_source(source_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    source = await session.get(Source, source_id)
    if source is None or source.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    return source


@router.post("/retrieve", response_model=list[RetrievalHit])
async def retrieve_chunks(
    data: RetrieveRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    """Hybrid-retrieve the most relevant chunks for a query, scoped to the learner."""
    return await retrieval.retrieve(
        session,
        llm,
        data.query,
        learner_id=learner.id,
        subject_id=data.subject_id,
        topic_id=data.topic_id,
        source_id=data.source_id,
        limit=data.limit,
    )


@router.get("/sources/{source_id}/chunks", response_model=list[ChunkRead])
async def get_source_chunks(source_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    source = await session.get(Source, source_id)
    if source is None or source.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    chunks = (
        await session.scalars(
            select(Chunk).where(Chunk.source_id == source_id).order_by(Chunk.ordinal)
        )
    ).all()
    return list(chunks)


@router.get("/chunks/{chunk_id}", response_model=ChunkRead)
async def get_chunk(chunk_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Fetch one chunk by id — backs the citation pane's click-through (Phase 7)."""
    chunk = await session.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk not found")
    source = await session.get(Source, chunk.source_id)
    if source is None or source.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk not found")
    return chunk
