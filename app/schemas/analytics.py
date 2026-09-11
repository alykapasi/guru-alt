"""Request/response schemas for the dashboard's analytics endpoints."""

import uuid

from pydantic import BaseModel


class KCMasteryRead(BaseModel):
    """One component's estimate, and whether the learner has ever been assessed on it.

    ``ability`` is logit-scale: 0 is the *prior*, not a measurement, and it is what an unseen
    component returns. Rendered through a sigmoid that reads 50%, which is why ``assessed``
    exists — a number derived from no evidence must not be displayed as if it were one.
    """

    kc_id: uuid.UUID
    kc_name: str
    ability: float
    uncertainty: float
    mastered: bool
    assessed: bool
    # What the estimate rests on (S14). The same number can come from four different problems
    # solved unaided over weeks or from one question answered four times in ten minutes, and
    # those are not the same claim — so the counts travel with it.
    distinct_items: int = 0
    unassisted_items: int = 0
    # Solved a *different* problem unaided, and demonstrated unaided after a delay. Both are
    # false for a component whose whole history is one question in one sitting.
    transfer_shown: bool = False
    retention_shown: bool = False


class TopicMasteryRead(BaseModel):
    """``assessed_kcs`` of ``total_kcs`` is the denominator a topic percentage is missing."""

    topic_id: uuid.UUID
    topic_name: str
    ability: float
    uncertainty: float
    mastered: bool
    assessed_kcs: int
    total_kcs: int
    kcs: list[KCMasteryRead]


class SubjectMasteryRead(BaseModel):
    subject_id: uuid.UUID
    ability: float
    uncertainty: float
    mastered: bool
    assessed_kcs: int
    total_kcs: int
    topics: list[TopicMasteryRead]


class ActivityRead(BaseModel):
    streak_days: int
    observations_last_7d: int
    observations_prior_7d: int
    momentum: str
    """"up" | "down" | "steady" | "none" — see app.learning.activity.momentum_trend."""
