"""Response schemas for the operational endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AlertTransitionRead(BaseModel):
    """One change of state for one alert condition (P11).

    ``detail`` and ``action`` are what the alert said *at the time*. A threshold retuned since
    would otherwise rewrite the history of every incident it was involved in.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    firing: bool
    severity: str | None
    detail: str | None
    action: str | None
    created_at: datetime
