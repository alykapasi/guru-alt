"""Request/response schemas for explicit learner preferences (S02)."""

import uuid
from typing import Literal

from pydantic import BaseModel


class PreferenceRead(BaseModel):
    """One setting: what it resolves to here, where that came from, and the alternatives."""

    key: str
    label: str
    value: str
    source: Literal["subject", "global", "default"]
    options: list[str]
    # The learner's global value, when this read is for a subject; else null.
    global_value: str | None
    # What adaptation currently chooses, for the keys inference covers; else null.
    inferred: str | None


class PreferenceSubmit(BaseModel):
    """Pin a setting at one level, or clear that level with null (a subject then follows the
    learner's default)."""

    value: str | None
    subject_id: uuid.UUID | None = None
