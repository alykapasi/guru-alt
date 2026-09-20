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


class PublicationReviewRead(BaseModel):
    """One request as the *reviewer* sees it: with the snapshot, and with who asked.

    The author is named here and nowhere a learner can reach (D6). Anonymity is towards other
    learners, not towards the person deciding — a reviewer judging material with no idea whose
    it is cannot weigh a pattern of requests from one account.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    author_handle: str | None
    author_note: str | None
    created_at: datetime
    reviewer_handle: str | None
    review_note: str | None
    reviewed_at: datetime | None
    published_subject_id: uuid.UUID | None
    snapshot: dict


class PublicationQueueRead(BaseModel):
    """The review queue, oldest first."""

    publications: list[PublicationReviewRead]


class ApproveRequest(BaseModel):
    """Which items the reviewer struck out, and why they approved."""

    excluded_item_ids: list[uuid.UUID] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)


class RejectRequest(BaseModel):
    """A refusal, with a reason the author can act on."""

    note: str = Field(min_length=1, max_length=2000)


class WithdrawRequest(BaseModel):
    """Unlisting a published subject, with the reason recorded."""

    reason: str = Field(min_length=1, max_length=2000)
