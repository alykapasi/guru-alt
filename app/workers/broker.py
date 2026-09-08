"""The taskiq broker seam.

A real Redis broker in dev/prod; an in-memory broker for tests (and any env without a
Redis URL), so job *dispatch* is exercisable offline. The job *logic* lives in services
(plain async functions); tasks (see ``tasks.py``) are thin wrappers, so most tests bypass
the broker entirely and call the service directly.
"""

from taskiq import AsyncBroker, InMemoryBroker
from taskiq_redis import ListQueueBroker

from app.core.config import AppEnv, Settings, get_settings


def build_broker(settings: Settings) -> AsyncBroker:
    """Redis-backed broker in dev/prod; in-memory otherwise (tests).

    ``socket_timeout=None`` is load-bearing, not tuning. The worker consumes with a blocking
    ``BRPOP`` that has no server-side timeout, so on an idle queue the read legitimately
    blocks forever — but redis-py 8 defaults ``socket_timeout`` to 5s, so the client aborted
    that read, raised ``redis.exceptions.TimeoutError`` out of taskiq's receiver task group,
    and killed the worker process. The supervisor restarted it, the queue was still idle, and
    it died again 5s later: an endless crash loop whenever nothing was being ingested, with
    any in-flight job lost. Keepalive plus a periodic health check keep a genuinely dead peer
    detectable without putting a deadline on the blocking read.
    """
    if settings.env is AppEnv.TEST or not settings.redis_url:
        return InMemoryBroker()
    return ListQueueBroker(
        settings.redis_url,
        socket_timeout=None,
        socket_keepalive=True,
        health_check_interval=30,
    )


broker: AsyncBroker = build_broker(get_settings())
