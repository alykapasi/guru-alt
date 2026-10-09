"""``poe perf-report``: time every hot path against a long history (S62).

Seeds ``guru_perf`` — its own database, never dev or test data — with one power user (five
times a year of daily use) and twenty ordinary learners, so tables and indexes look like a
shared database rather than one person's. Then runs each path in ``tests.perf.paths`` in
process: warm-ups, then timed runs, with every statement timed. A report, not a gate: it always
exits 0, and a path over budget is marked for a person to read.

    uv run poe perf-report [--reseed] [--runs N] [--json PATH]
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

SEED_VERSION = 1
BUDGET_MS = 250.0


@dataclass
class PathResult:
    name: str
    p50_ms: float
    p95_ms: float
    max_ms: float
    statements: int
    rows: int
    slowest: list[tuple[float, str]] = field(default_factory=list)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


async def measure(
    engine: AsyncEngine,
    history: Any,
    paths: dict[str, Callable[[AsyncSession, Any], Awaitable[object]]],
    *,
    warmup: int,
    runs: int,
) -> list[PathResult]:
    """Run each path ``warmup`` times untimed, then ``runs`` times timed, one session per run.

    Statements and rows are those of the last timed run; latency is every timed run's.
    """
    sync_engine = engine.sync_engine
    results = []
    for name, path in paths.items():
        timings: list[float] = []
        last: list[tuple[float, str, int]] = []
        for i in range(warmup + runs):
            run: list[tuple[float, str, int]] = []

            def before(conn, cursor, statement, parameters, context, executemany):
                context._s62_start = time.perf_counter()

            def after(conn, cursor, statement, parameters, context, executemany, run=run):
                elapsed = (time.perf_counter() - context._s62_start) * 1000
                run.append((elapsed, statement.split("\n")[0][:140], cursor.rowcount))

            event.listen(sync_engine, "before_cursor_execute", before)
            event.listen(sync_engine, "after_cursor_execute", after)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    start = time.perf_counter()
                    await path(session, history)
                    await session.commit()
                    elapsed = (time.perf_counter() - start) * 1000
            finally:
                event.remove(sync_engine, "before_cursor_execute", before)
                event.remove(sync_engine, "after_cursor_execute", after)
            if i >= warmup:
                timings.append(elapsed)
                last = run
        results.append(
            PathResult(
                name=name,
                p50_ms=statistics.median(timings),
                p95_ms=_p95(timings),
                max_ms=max(timings),
                statements=len(last),
                rows=sum(max(n, 0) for _, _, n in last),
                slowest=[(ms, s) for ms, s, _ in sorted(last, reverse=True)[:3]],
            )
        )
    return results


def render(results: list[PathResult], *, budget_ms: float = BUDGET_MS) -> str:
    lines = [
        "| path | p50 ms | p95 ms | max ms | statements | rows | budget |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in results:
        flag = f"over {budget_ms:.0f}" if r.p95_ms > budget_ms else "ok"
        lines.append(
            f"| {r.name} | {r.p50_ms:.1f} | {r.p95_ms:.1f} | {r.max_ms:.1f} | "
            f"{r.statements} | {r.rows} | {flag} |"
        )
    lines.append("")
    lines.append("Slowest statements (one timed run):")
    for r in results:
        lines.append(f"- {r.name}: " + "; ".join(f"{ms:.1f} ms `{s}`" for ms, s in r.slowest))
    return "\n".join(lines)


@asynccontextmanager
async def _discard_accounting() -> AsyncIterator[Any]:
    class _Discarded:
        def add(self, obj: object) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def execute(self, *args: object, **kwargs: object) -> None:
            pass

    yield _Discarded()


async def _seed(engine: AsyncEngine, *, reseed: bool) -> Any:
    from app.models.learner import Learner
    from tests.history import ORDINARY, POWER_USER, SeededHistory, seed_history

    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS perf_seed (version int PRIMARY KEY, payload jsonb)")
        )
        row = (
            await conn.execute(
                text("SELECT payload FROM perf_seed WHERE version = :v"), {"v": SEED_VERSION}
            )
        ).first()
    if row is not None and not reseed:
        payload = row[0]
        return SeededHistory(**{k: uuid.UUID(v) for k, v in payload.items()})
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(text("DELETE FROM learners WHERE handle LIKE 'perf-%'"))
        await session.execute(text("DELETE FROM perf_seed"))
        await session.commit()
        for i in range(20):
            other = Learner(handle=f"perf-ordinary-{i}")
            session.add(other)
            await session.commit()
            await seed_history(session, other.id, ORDINARY)
            print(f"seeded ordinary learner {i + 1}/20", file=sys.stderr)
        power = Learner(handle="perf-power-user")
        session.add(power)
        await session.commit()
        history = await seed_history(session, power.id, POWER_USER)
        await session.execute(
            text("INSERT INTO perf_seed (version, payload) VALUES (:v, CAST(:p AS jsonb))"),
            {"v": SEED_VERSION, "p": json.dumps({k: str(v) for k, v in asdict(history).items()})},
        )
        await session.commit()
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("ANALYZE"))
    return history


async def _main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="poe perf-report")
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)

    from app.services.llm_log import set_accounting_session_factory
    from tests.perf.paths import PATHS

    set_accounting_session_factory(_discard_accounting)
    # The table is stdout's; the app's own logs (every fake call, at info) would bury it.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
    engine = create_async_engine(os.environ["GURU_DATABASE_URL"])
    try:
        history = await _seed(engine, reseed=args.reseed)
        results = await measure(engine, history, PATHS, warmup=3, runs=args.runs)
    finally:
        await engine.dispose()
    print(render(results))
    if args.json_path:
        with open(args.json_path, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)


def main() -> None:
    from app.core.config import Settings
    from tests.testdb import test_database_url

    url = test_database_url(Settings().database_url, "_perf")
    subprocess.run([sys.executable, "-m", "tests.testdb", "--suffix", "_perf"], check=True)
    # Before anything reads settings: the app's own engine and caps must see this database.
    os.environ["GURU_DATABASE_URL"] = url
    asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
