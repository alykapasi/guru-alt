"""Request/response schemas for the dashboard's analytics endpoints."""

import uuid

from pydantic import BaseModel


class KCMasteryRead(BaseModel):
    kc_id: uuid.UUID
    kc_name: str
    ability: float
    uncertainty: float
    mastered: bool


class TopicMasteryRead(BaseModel):
    topic_id: uuid.UUID
    topic_name: str
    ability: float
    uncertainty: float
    mastered: bool
    kcs: list[KCMasteryRead]


class SubjectMasteryRead(BaseModel):
    subject_id: uuid.UUID
    ability: float
    uncertainty: float
    mastered: bool
    topics: list[TopicMasteryRead]


class ActivityRead(BaseModel):
    streak_days: int
    observations_last_7d: int
    observations_prior_7d: int
    momentum: str
    """"up" | "down" | "steady" | "none" — see app.learning.activity.momentum_trend."""
