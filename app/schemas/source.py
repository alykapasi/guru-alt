"""Request/response schemas for ingestion sources and their chunks."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class LinkCreate(BaseModel):
    """Request to ingest a public web page."""

    url: HttpUrl
    subject_id: uuid.UUID | None = None
    topic_id: uuid.UUID | None = None


class RetrieveRequest(BaseModel):
    """A hybrid-retrieval query, optionally scoped to a subject/topic/source."""

    query: str = Field(min_length=1)
    subject_id: uuid.UUID | None = None
    topic_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SourceRead(BaseModel):
    """An ingestion source and its current status."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    origin: str
    content_type: str | None
    status: str
    error: str | None
    subject_id: uuid.UUID | None
    topic_id: uuid.UUID | None
    created_at: datetime


class ChunkRead(BaseModel):
    """A stored chunk as exposed for debugging — text + provenance, not the raw vector."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ordinal: int
    text: str
    provenance: dict
