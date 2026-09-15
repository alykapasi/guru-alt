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


async def gather_bounded_settled[T](
    awaitables: Sequence[Awaitable[T]], limit: int
) -> list[T | BaseException]:
    """Like :func:`gather_bounded`, but a failure is returned in place, not propagated.

    ``asyncio.gather`` abandons its siblings on the first exception: the other calls are left
    running and their results — including what they cost — are discarded. That is the wrong
    shape wherever the work has already been paid for, because the caller never learns which
    of the fan-out succeeded. Here every awaitable is settled and the caller decides what a
    partial result means.
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def _run(aw: Awaitable[T]) -> T:
        async with sem:
            return await aw

    return await asyncio.gather(*(_run(aw) for aw in awaitables), return_exceptions=True)
