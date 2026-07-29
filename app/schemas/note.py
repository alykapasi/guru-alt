"""Request/response schemas for notes."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NoteFormat = Literal["outline", "narrative", "mnemonic", "worked_examples"]


class NoteRead(BaseModel):
    topic_id: uuid.UUID
    content_md: str | None
    format: NoteFormat | None
    effective_format: NoteFormat
    stale: bool
    revision_ordinal: int | None
    updated_at: datetime | None


class NoteIndexEntry(BaseModel):
    topic_id: uuid.UUID
    topic_name: str
    has_note: bool
    stale: bool
    updated_at: datetime | None


class NoteEditRequest(BaseModel):
    content_md: str = Field(min_length=1)


class NoteFormatRequest(BaseModel):
    format: NoteFormat | None  # None = back to auto


class NoteRevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    cause: str
    created_at: datetime


class NoteRevisionSource(BaseModel):
    ordinal: int
    content_md: str
