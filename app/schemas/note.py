"""Request/response schemas for notes."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NoteFormat = Literal["outline", "narrative", "mnemonic", "worked_examples"]


class NoteRead(BaseModel):
    topic_id: uuid.UUID
    content_md: str | None
    learner_authored_md: str | None = None
    generated_md: str | None = None
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
    """A learner's exact Markdown, optionally adopting the generated display.

    ``expected_revision_ordinal`` is the revision they were looking at. Sending it turns a
    concurrent change — a refresh, or the same note open in another tab — into a 409 instead of
    an edit silently absorbed against a note that no longer looks like what they edited.
    """

    include_generated: bool = True
    content_md: str = Field(min_length=1)
    expected_revision_ordinal: int | None = Field(default=None, ge=1)


class NoteFormatRequest(BaseModel):
    format: NoteFormat | None  # None = back to auto


class NoteRevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    cause: str
    created_at: datetime


class NoteRevisionSource(BaseModel):
    """A revision's exact authored text and independent generated surroundings.

    ``content_md`` composes the saved authored text with mechanical generated content;
    ``learner_edit_md`` is the raw submission on an edit revision, otherwise null.
    """

    ordinal: int
    content_md: str
    learner_edit_md: str | None = None


class NoteRestoreRequest(BaseModel):
    expected_revision_ordinal: int = Field(ge=1)
