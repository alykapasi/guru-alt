"""Request and response bodies for authentication (S21)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH


class RegisterRequest(BaseModel):
    """Create an account."""

    email: EmailStr
    # A length floor only. Composition rules ("one capital, one symbol") push people towards
    # predictable substitutions and away from length, which is the property that matters.
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    display_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    """Exchange credentials for a session."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class LearnerRead(BaseModel):
    """The learner a session belongs to."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    handle: str
    display_name: str | None
    email: str | None


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
