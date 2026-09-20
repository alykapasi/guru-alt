"""Request and response bodies for publication (S25b)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PublicationRequest(BaseModel):
    """What an author says when asking for their subject to be shared."""

    note: str | None = Field(default=None, max_length=2000)


class PublicationRead(BaseModel):
    """One request, as its author sees it.

    Deliberately without the snapshot. The author already has the subject it was taken from,
    and a response that repeated the whole graph on every poll of a status would be paying for
    it every time. The reviewer's view carries it; see `app.api.v1.admin`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    author_note: str | None
    review_note: str | None
    reviewed_at: datetime | None
    created_at: datetime
    # Set once approved. The author's link to what actually shipped.
    published_subject_id: uuid.UUID | None


class PublicationListRead(BaseModel):
    """A subject's publication history, newest first."""

    publications: list[PublicationRead]
