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
    """Redis-backed broker in dev/prod; in-memory otherwise (tests)."""
    if settings.env is AppEnv.TEST or not settings.redis_url:
        return InMemoryBroker()
    return ListQueueBroker(settings.redis_url)


broker: AsyncBroker = build_broker(get_settings())
