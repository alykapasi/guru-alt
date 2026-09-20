"""Request and response bodies for authentication (S21)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class LearnerRead(BaseModel):
    """The learner a session belongs to."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    handle: str
    display_name: str | None
    email: str | None
    # Exposed because the app has to know whether to offer the portal at all (P10). It is not
    # what authorizes anything — the API refuses a non-administrator whatever the browser
    # renders — it is what keeps a learner from being shown a door that answers 403.
    is_admin: bool = False


class SessionRead(BaseModel):
    """One live session, as its owner sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool = False


class SessionListRead(BaseModel):
    """Every live session for the current learner."""

    sessions: list[SessionRead]


class DevLoginRequest(BaseModel):
    """Who the development sign-in should sign in as (S21).

    Optional: with no address it signs in as the one shared development learner, as it always
    has. With one, it signs in as that address' account and creates it if it does not exist —
    which is how the browser journeys get a fresh account each run now that there is no
    registration form for them to drive.
    """

    email: EmailStr | None = None
