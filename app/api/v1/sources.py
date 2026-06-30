"""Ingestion endpoints: upload a source, then poll its status / inspect its chunks."""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select

from app.api.deps import (
    BlobStoreDep,
    CurrentLearner,
    IngestionEnqueuerDep,
    LLMClientDep,
    SessionDep,
)
from app.models.source import Chunk, Source, SourceKind
from app.rag import retrieval
from app.rag.retrieval import RetrievalHit
from app.schemas.source import ChunkRead, LinkCreate, RetrieveRequest, SourceRead
from app.services import ingestion as svc

router = APIRouter(tags=["sources"])


@router.post("/sources/upload", response_model=SourceRead, status_code=status.HTTP_202_ACCEPTED)
async def upload_source(
    session: SessionDep,
    learner: CurrentLearner,
    blobstore: BlobStoreDep,
    enqueue: IngestionEnqueuerDep,
    file: Annotated[UploadFile, File()],
    subject_id: Annotated[uuid.UUID | None, Form()] = None,
    topic_id: Annotated[uuid.UUID | None, Form()] = None,
):
    """Store an uploaded file, create a pending source, and queue ingestion."""
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
    source = await svc.create_source(
        session,
        blobstore,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=file.filename or "upload",
        content_type=file.content_type,
        data=data,
        subject_id=subject_id,
        topic_id=topic_id,
    )
    await enqueue(source.id)
    return source


@router.post("/sources/link", response_model=SourceRead, status_code=status.HTTP_202_ACCEPTED)
async def link_source(
    data: LinkCreate,
    session: SessionDep,
    learner: CurrentLearner,
    enqueue: IngestionEnqueuerDep,
):
    """Register a web page for ingestion. The page is fetched in the background job."""
    source = await svc.create_url_source(
        session,
        learner_id=learner.id,
        url=str(data.url),
        subject_id=data.subject_id,
        topic_id=data.topic_id,
    )
    await enqueue(source.id)
    return source


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
