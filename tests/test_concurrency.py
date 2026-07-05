"""`gather_bounded` — bounded-concurrency fan-out used by concurrent OCR and batched embeds."""

import asyncio

from app.rag.concurrency import gather_bounded


async def test_gather_bounded_preserves_input_order() -> None:
    async def make(i: int) -> int:
        # Later items sleep less, so completion order != input order.
        await asyncio.sleep((6 - i) * 0.002)
        return i

    result = await gather_bounded([make(i) for i in range(6)], limit=3)
    assert result == [0, 1, 2, 3, 4, 5]


async def test_gather_bounded_never_exceeds_limit() -> None:
    inflight = 0
    max_inflight = 0

    async def task() -> None:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        try:
            await asyncio.sleep(0.02)
        finally:
            inflight -= 1

    await gather_bounded([task() for _ in range(6)], limit=2)
    assert max_inflight == 2  # capped at the limit, yet genuinely concurrent (>1)


async def test_gather_bounded_empty_returns_empty() -> None:
    assert await gather_bounded([], limit=3) == []
