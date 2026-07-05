"""Bounded-concurrency fan-out for ingestion.

Big documents want many network/CPU tasks in flight — scanned-PDF pages to vision-OCR,
chunk batches to embed — but not *unbounded*: a 500-page textbook must not open 500 model
calls at once. :func:`gather_bounded` runs awaitables concurrently under a semaphore cap and
returns results **in input order** (like :func:`asyncio.gather`). DB writes are never fanned
out this way — a single ``AsyncSession`` is not concurrency-safe; only stateless work is.
"""

import asyncio
from collections.abc import Awaitable, Sequence


async def gather_bounded[T](awaitables: Sequence[Awaitable[T]], limit: int) -> list[T]:
    """Await all ``awaitables`` with at most ``limit`` running at once; keep input order."""
    sem = asyncio.Semaphore(max(1, limit))

    async def _run(aw: Awaitable[T]) -> T:
        async with sem:
            return await aw

    return await asyncio.gather(*(_run(aw) for aw in awaitables))
