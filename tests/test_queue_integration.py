"""Delivery through a real Redis broker, not an in-memory stand-in (S58).

The suite's default broker is `InMemoryBroker`, which proves the wiring and nothing about
delivery: it never serialises a message, never writes it anywhere, and never reads it back on
another connection. A change that broke any of those would pass the whole suite.

Opt-in — see `tests/queue_broker` — because a test that depends on what happens to be running
on a laptop makes a green local run mean less than a green CI run.
"""

import asyncio

import pytest
from taskiq import InMemoryBroker
from taskiq_redis import ListQueueBroker

from app.core.config import AppEnv, get_settings
from app.workers.broker import build_broker
from tests.queue_broker import SKIP_REASON, enabled, reachable, suite_redis_url

pytestmark = pytest.mark.skipif(not enabled(), reason=SKIP_REASON)


@pytest.fixture
def redis_url() -> str:
    url = suite_redis_url(get_settings().redis_url or "redis://localhost:6379/0")
    if not reachable(url):
        pytest.skip(f"no Redis listening at {url}")
    return url


async def _drain(broker: ListQueueBroker) -> None:
    """Empty the queue so one test's leftovers cannot be another's first message."""
    with_timeout = broker.listen().__aiter__()
    while True:
        try:
            await asyncio.wait_for(with_timeout.__anext__(), timeout=0.2)
        except (TimeoutError, StopAsyncIteration):
            return


async def test_a_dispatched_task_comes_back_off_a_real_queue(redis_url: str) -> None:
    """The round trip the in-memory broker cannot make: serialise, write to Redis, read back.

    Asserting on the task *name and arguments* rather than merely that bytes arrived — a
    message that survives the trip but loses its arguments is the failure that would actually
    reach production, and it looks identical from the dispatch side.
    """
    broker = ListQueueBroker(redis_url)

    async def _job(source_id: str) -> None: ...

    job = broker.task(task_name="queue_integration_probe")(_job)
    await broker.startup()
    try:
        await _drain(broker)
        await job.kiq("source-42")

        raw = await asyncio.wait_for(broker.listen().__aiter__().__anext__(), timeout=5.0)
    finally:
        await broker.shutdown()

    body = raw.decode() if isinstance(raw, bytes) else str(raw)
    assert "queue_integration_probe" in body, "the task name survived the trip"
    assert "source-42" in body, "and so did its argument"


async def test_the_real_broker_is_what_production_builds(redis_url: str) -> None:
    """`build_broker` is the seam, and the thing worth checking about it is that a non-test
    environment with a Redis URL gets Redis — not that the in-memory fallback works, which is
    all the rest of the suite ever exercises."""
    # `model_copy`, not `Settings(**model_dump())`: the latter re-validates every field from
    # its dumped form and `ty` cannot see that the round trip is type-preserving.
    base = get_settings()
    prod = build_broker(base.model_copy(update={"env": AppEnv.DEV, "redis_url": redis_url}))
    tests = build_broker(base.model_copy(update={"env": AppEnv.TEST, "redis_url": redis_url}))

    assert isinstance(prod, ListQueueBroker)
    assert isinstance(tests, InMemoryBroker), "the suite never reaches a real queue by accident"
