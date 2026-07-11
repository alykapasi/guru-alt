"""The learner-profile dimension catalog: pure/LLM estimators over a learner's history.

Same shape as ``kc_tagging.py``/``placement_inference.py``: numbered-list prompts for the
LLM-backed estimators, tolerant parsing, best-effort (an estimator that can't produce a
confident value returns ``None`` rather than fabricating one). ``DIMENSION_SPECS`` is the
single source of truth for the dimension catalog — adding or dropping a dimension is a change
to this list only, never a migration (see ``app/models/profile.py``).
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from app.models.chat import Message
from app.models.learning import LearningEvent

Kind = Literal["trait", "state"]
Source = Literal["behavioral", "self_report"]


@dataclass(frozen=True)
class DimensionEstimate:
    """One estimator's output: a JSON-serializable value plus its uncertainty."""

    value: Any
    uncertainty: float


@dataclass(frozen=True)
class EstimatorContext:
    """Everything an estimator might need, pre-loaded once and shared across the catalog.

    ``events``/``messages`` cover the learner's full history — trait estimators read them
    as-is, state estimators (e.g. ``engagement``) window down to the most recent session
    themselves. ``session``/``llm`` are here for the minority of estimators that need an
    extra DB lookup (``format_effectiveness`` joins to ``Item``) or a model call.
    """

    session: AsyncSession
    learner_id: uuid.UUID
    events: Sequence[LearningEvent]
    messages: Sequence[Message]
    llm: LLMClient


EstimatorFn = Callable[[EstimatorContext], Awaitable[DimensionEstimate | None]]


@dataclass(frozen=True)
class DimensionSpec:
    """One catalog entry: a dimension key plus the estimator that computes it."""

    key: str
    kind: Kind
    source: Source
    estimate: EstimatorFn


DIMENSION_SPECS: list[DimensionSpec] = []
"""The dimension catalog. Populated incrementally (see commits 2-4 of the learner-profile
slice) — each family's estimators are appended here as they're implemented."""


def _uncertainty(n: int, *, floor: float = 0.15) -> float:
    """Shrink uncertainty as evidence accumulates. Same v1-heuristic spirit as placement's
    ability/uncertainty mapping — not calibrated, revisit once real data exists."""
    return max(floor, 1.0 / (1.0 + 0.3 * n))


def _cluster_sessions(
    events: Sequence[LearningEvent], *, gap_minutes: int = 30
) -> list[list[LearningEvent]]:
    """Split a chronological event stream into sessions wherever the gap exceeds ``gap_minutes``.

    A simple, well-established heuristic (no session concept exists in the schema); shared by
    every estimator that needs session-scoped or session-aggregated behavior.
    """
    ordered = sorted(events, key=lambda e: e.created_at)
    if not ordered:
        return []
    sessions: list[list[LearningEvent]] = [[ordered[0]]]
    for prev, curr in pairwise(ordered):
        gap = (curr.created_at - prev.created_at).total_seconds() / 60.0
        if gap > gap_minutes:
            sessions.append([])
        sessions[-1].append(curr)
    return sessions
