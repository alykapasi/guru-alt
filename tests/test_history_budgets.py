"""No hot path costs more because the learner has been here longer (S62).

Each path runs for a learner with a small history and for one with four times as much, on the
same graph. Statements and rows must not grow; the slack of two absorbs a conditional branch,
not growth. Paths the first run found growing are marked for S62 part B with the measured
cause, and the marker is strict: fixing one without removing it fails the suite.
"""

import uuid
from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.learner import Learner
from tests.history import SMALL, seed_history
from tests.perf import paths
from tests.perf.paths import PATHS
from tests.querycount import count_queries

PART_B: dict[str, str] = {}
"""Path → measured cause, for paths that grow and are fixed in S62 part B."""


@pytest.fixture(autouse=True)
def small_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every page and window smaller than the small history, so both histories fill it.

    Otherwise a bounded read looks like growth: six conversations against twenty-one is more
    rows only because neither reached the page size.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "chat_conversation_page_size", 3)
    monkeypatch.setattr(settings, "sources_page_size", 2)
    monkeypatch.setattr(settings, "chat_transcript_page_size", 10)
    monkeypatch.setattr(settings, "chat_history_max_messages", 10)
    monkeypatch.setattr(settings, "profile_event_window", 100)
    monkeypatch.setattr(settings, "profile_message_window", 50)
    monkeypatch.setattr(paths, "MEMORY_PAGE", 10)


async def _seeded(session: AsyncSession, scale: int):
    learner = Learner(handle=f"hb-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    # Pooled vectors: budgets count statements and rows, which do not depend on them.
    return await seed_history(session, learner.id, replace(SMALL, vector_pool=8).scaled(scale))


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, marks=pytest.mark.xfail(strict=True, reason=PART_B[n]))
        if n in PART_B
        else n
        for n in sorted(PATHS)
    ],
)
async def test_a_hot_path_does_not_grow_with_history(name: str, db_session: AsyncSession) -> None:
    small = await _seeded(db_session, 1)
    large = await _seeded(db_session, 4)
    path = PATHS[name]

    with count_queries(db_session) as s:
        await path(db_session, small)
    with count_queries(db_session) as big:
        await path(db_session, large)

    assert len(big) <= len(s) + 2, f"{name}: statements grew\nsmall {s!r}\nlarge {big!r}"
    assert big.rows <= s.rows + 2, f"{name}: rows grew\nsmall {s!r}\nlarge {big!r}"
