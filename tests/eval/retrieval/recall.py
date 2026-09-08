"""Does the vector arm return the right rows, and what does it cost to do so? (S76)

Retrieval's vector arm asks Postgres for nearest neighbours *within one learner's chunks*.
Whether that is answered exactly or approximately is the planner's choice, not ours, and the
two behave very differently — so this measures the choice rather than assuming it:

  * which plan the scoped query actually gets, at a given corpus size
  * its recall against the same query forced onto an exact scan
  * what the HNSW index would give instead, across ``hnsw.ef_search``

It seeds its own throwaway database (``<db>_recall``) because the answer depends on table
statistics, and it needs tens of thousands of rows to be meaningful — far past what belongs in
the test suite. Paid in time, not tokens: no model is called, the vectors are synthetic.

**Read the recall numbers as a lower bound.** The vectors here are hash-derived and close to
uniform, which is roughly worst case for a graph index; real embeddings cluster, and cluster
structure is what HNSW exploits. This says how the *plan* behaves and how the trade is shaped,
not what recall a real corpus would get.

    uv run poe retrieval-recall              # default 40k rows
    uv run poe retrieval-recall --rows 5000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import random
import time
import uuid
from dataclasses import dataclass

import asyncpg
from sqlalchemy.engine import make_url

from app.core.config import Settings

SPACE = "fake:fake-1:768"
BATCH = 1_000
PLANTED = 50
"""Rows seeded near the query vector, so "the right answer" is not a matter of degree."""

# app/rag/retrieval.py's vector arm, as it is actually written.
SCOPED = (
    "SELECT c.id FROM chunks c JOIN sources s ON c.source_id = s.id "
    f"WHERE s.learner_id = $1 AND c.embedding_space = '{SPACE}' "
    "ORDER BY c.embedding <=> $2::vector LIMIT $3"
)
# The same rows, addressed without the join — which is what lets the planner reach the index.
VIA_INDEX = (
    "SELECT c.id FROM chunks c "
    f"WHERE c.source_id = ANY($1::uuid[]) AND c.embedding_space = '{SPACE}' "
    "ORDER BY c.embedding <=> $2::vector LIMIT $3"
)


@dataclass(frozen=True)
class Timed:
    rows: set[uuid.UUID]
    ms: float
    plan: str

    @property
    def uses_index(self) -> bool:
        return "ix_chunks_embedding_hnsw" in self.plan


def _url(suffix: str = "_recall") -> str:
    """This measurement's own database, derived from the configured one (as tests.testdb does).

    ``Settings()`` rather than ``get_settings()``: the cached accessor would fix the settings
    for the whole process at the *configured* database, and alembic reads them — which would
    quietly migrate the developer's own database instead of this throwaway one.
    """
    url = make_url(Settings().database_url)
    name = url.database or "guru"
    if name.endswith(suffix):  # idempotent: _bootstrap points the env at this database
        return url.render_as_string(hide_password=False)
    return url.set(database=f"{name}{suffix}").render_as_string(hide_password=False)


def _dsn() -> str:
    return _url().replace("postgresql+asyncpg://", "postgresql://")


def _bootstrap() -> None:
    """Create the database if missing and bring it to head — it holds nothing worth keeping.

    Synchronous, and called before any event loop of ours exists: alembic's ``env.py`` runs
    ``asyncio.run`` itself and cannot be invoked from inside a running loop.
    """
    from tests.testdb import _create_if_missing

    url = _url()
    asyncio.run(_create_if_missing(url))
    os.environ["GURU_DATABASE_URL"] = url
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")


def _vec(text: str, dim: int) -> str:
    digest = hashlib.sha256(text.encode()).digest()
    return "[" + ",".join(f"{digest[i % len(digest)] / 255.0:.6f}" for i in range(dim)) + "]"


def _noise(rng: random.Random, dim: int) -> str:
    return "[" + ",".join(f"{rng.random():.6f}" for _ in range(dim)) + "]"


async def _seed(conn: asyncpg.Connection, handle: str) -> tuple[uuid.UUID, uuid.UUID]:
    learner_id, source_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO learners (id, handle, created_at, updated_at) VALUES ($1,$2,now(),now())",
        learner_id,
        handle,
    )
    await conn.execute(
        "INSERT INTO sources (id, learner_id, kind, origin, status, meta, created_at, updated_at)"
        " VALUES ($1,$2,'file','recall','done','{}',now(),now())",
        source_id,
        learner_id,
    )
    return learner_id, source_id


async def _add(conn: asyncpg.Connection, source_id: uuid.UUID, vectors: list[str], at: int) -> None:
    await conn.executemany(
        "INSERT INTO chunks (id, source_id, ordinal, text, embedding, embedding_space,"
        f" provenance, created_at, updated_at)"
        f" VALUES ($1,$2,$3,$4,$5::vector,'{SPACE}','{{}}',now(),now())",
        [(uuid.uuid4(), source_id, at + i, f"p{at + i}", v) for i, v in enumerate(vectors)],
    )


async def _timed(conn: asyncpg.Connection, sql: str, *args) -> Timed:
    plan = "\n".join(r["QUERY PLAN"] for r in await conn.fetch("EXPLAIN " + sql, *args))
    await conn.fetch(sql, *args)  # warm
    runs: list[float] = []
    for _ in range(3):
        start = time.perf_counter()
        rows = await conn.fetch(sql, *args)
        runs.append((time.perf_counter() - start) * 1000)
    runs.sort()
    return Timed(rows={r["id"] for r in rows}, ms=runs[1], plan=plan)


async def measure(rows: int, limit: int, dim: int) -> None:
    rng = random.Random(7)
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("TRUNCATE chunks, sources, learners CASCADE")
        learner_id, source_id = await _seed(conn, f"recall-{uuid.uuid4().hex[:8]}")
        query = _vec("needle", dim)
        await _add(conn, source_id, [_vec(f"needle {i}", dim) for i in range(PLANTED)], 0)
        for at in range(PLANTED, rows, BATCH):
            await _add(conn, source_id, [_noise(rng, dim) for _ in range(BATCH)], at)
        await conn.execute("ANALYZE chunks")

        total = await conn.fetchval("SELECT count(*) FROM chunks")
        source_ids = [source_id]
        print(f"{total} chunks, all owned by one learner · asking for {limit}\n")

        scoped = await _timed(conn, SCOPED, learner_id, query, limit)
        async with conn.transaction():
            await conn.execute("SET LOCAL enable_indexscan = off")
            await conn.execute("SET LOCAL enable_bitmapscan = off")
            forced = await _timed(conn, SCOPED, learner_id, query, limit)
        agreement = len(scoped.rows & forced.rows)
        print(
            f"  as written        plan={'HNSW' if scoped.uses_index else 'exact':<5} "
            f"recall={agreement}/{len(forced.rows)}  {scoped.ms:7.1f} ms"
        )

        print("\n  reachable through the index (no join), against the exact answer:")
        for ef in (40, 100, 200, 400, 1_000):
            async with conn.transaction():
                await conn.execute(f"SET LOCAL hnsw.ef_search = {ef}")
                via = await _timed(conn, VIA_INDEX, source_ids, query, limit)
            hit = len(via.rows & forced.rows)
            note = "" if via.uses_index else "   (planner declined the index)"
            print(
                f"    ef_search={ef:>5}  recall={hit:>3}/{len(forced.rows)} "
                f"({hit / max(len(forced.rows), 1):5.0%})  {via.ms:7.1f} ms{note}"
            )
    finally:
        await conn.execute("TRUNCATE chunks, sources, learners CASCADE")
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=40_000, help="chunks to seed (default 40000)")
    parser.add_argument(
        "--limit", type=int, default=50, help="rows requested — retrieval's `candidates`"
    )
    args = parser.parse_args()
    _bootstrap()
    asyncio.run(measure(args.rows, args.limit, Settings().embed_dim))


if __name__ == "__main__":
    main()
