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


class PasswordChange(BaseModel):
    """Set a new password, proving you know the current one.

    The current password is required even though the caller already holds a valid session: a
    session is not proof of the person, and a borrowed laptop is a session.
    """

    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class EmailChange(BaseModel):
    """Move to a new address. The password is the proof, for the same reason as above."""

    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    email: EmailStr


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class SessionListRead(BaseModel):
    """Every live session for the current learner."""

    sessions: list[SessionRead]
