"""Request and response bodies for the operator's portal (P10)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.services.impersonation import MIN_REASON_LENGTH


class ImpersonationRequest(BaseModel):
    """Ask to view one learner's account, and say why.

    The reason is required by the schema rather than checked later, so a request without one
    never reaches the code that issues a credential. Nothing here can tell a real reason from
    a plausible one; what the floor enforces is that somebody had to type a sentence next to
    their own name first.
    """

    learner_id: uuid.UUID
    reason: str = Field(min_length=MIN_REASON_LENGTH, max_length=500)


class ImpersonationRead(BaseModel):
    """One recorded visit.

    The ids are nullable and the handles are not, which is the record outliving the accounts:
    closing either one clears its id and leaves the handle, so a row still says who did what
    rather than becoming two empty columns. ``ended_at`` means *explicitly* ended — a visit
    nobody closed simply expires, and ``expires_at`` is the outer bound either way.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admin_learner_id: uuid.UUID | None
    admin_handle: str
    learner_id: uuid.UUID | None
    learner_handle: str | None
    reason: str
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None


class ImpersonationStarted(BaseModel):
    """The credential, returned once, and the record that was written with it."""

    token: str
    expires_at: datetime
    learner_id: uuid.UUID
    learner_handle: str
    impersonation: ImpersonationRead


class AdminActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    impersonation_id: uuid.UUID
    method: str
    route: str
    resource_ids: dict[str, str]
    status_code: int | None
    created_at: datetime
    completed_at: datetime | None
