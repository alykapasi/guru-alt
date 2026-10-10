"""The perf report measures each path and marks the ones over budget (S62)."""

import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.chat import LLMCall
from app.models.learner import Learner
from tests.history import SMALL, seed_history
from tests.perf.paths import PATHS
from tests.perf.report import PathResult, measure, render


def test_render_marks_paths_over_budget() -> None:
    fast = PathResult("memory_list", 2.0, 3.0, 4.0, 1, 30, [(1.5, "SELECT memories")])
    slow = PathResult("turn", 180.0, 310.0, 400.0, 22, 900, [(120.0, "SELECT chunks")])
    table = render([fast, slow], budget_ms=250.0)
    assert "| turn |" in table and "over" in table.split("| turn |")[1].split("\n")[0]
    assert "over" not in table.split("| memory_list |")[1].split("\n")[0]
    assert "SELECT chunks" in table


async def test_measure_times_each_path(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        learner = Learner(handle=f"pr-{uuid.uuid4().hex[:8]}")
        session.add(learner)
        await session.commit()
        history = await seed_history(session, learner.id, SMALL)
    try:
        results = await measure(
            engine, history, {"memory_list": PATHS["memory_list"]}, warmup=1, runs=3
        )
        (r,) = results
        assert r.name == "memory_list" and r.p50_ms > 0 and r.statements >= 1 and r.rows >= 1
        assert r.p50_ms <= r.p95_ms <= r.max_ms
    finally:
        async with AsyncSession(engine) as session:
            from app.services import retention
            from app.storage.memory import InMemoryBlobStore

            # Cost rows outlive an account by design (anonymised), so a test that seeds them
            # removes them itself rather than leaving them in the shared test database.
            await session.execute(delete(LLMCall).where(LLMCall.learner_id == history.learner_id))
            await retention.delete_learner(session, InMemoryBlobStore(), history.learner_id)
