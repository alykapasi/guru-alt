"""Readiness: can this process actually do its job right now? (S60)

Distinct from ``/health``, which answers "is this process alive" and must stay trivial so a
supervisor does not restart a healthy container because a dependency blipped. Readiness answers
"should this instance receive traffic", so it talks to the dependencies a request needs.

Every probe is bounded. An unbounded readiness check is worse than none: the probe hangs, the
orchestrator's own timeout fires, and the instance is marked unready for a reason nobody can
read. A timeout here is a *result* — reported as its own failure, with the limit named.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.base import BlobNotFound, BlobStore

log = structlog.get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 2.0
"""Per-dependency budget. Short on purpose: this runs on every orchestrator poll."""

# A key that cannot exist. Asking for it proves the store answered us — "not found" is a
# successful round trip, and the only alternative (writing a probe object) makes a readiness
# check a source of garbage.
_PROBE_KEY = "healthcheck/__readiness_probe__"


class DependencyStatus(BaseModel):
    name: str
    ok: bool
    latency_ms: float
    detail: str | None = None


class ReadinessReport(BaseModel):
    ready: bool
    dependencies: list[DependencyStatus]


async def _probe(name: str, check: Callable[[], Awaitable[None]]) -> DependencyStatus:
    started = time.monotonic()
    try:
        await asyncio.wait_for(check(), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return DependencyStatus(
            name=name,
            ok=False,
            latency_ms=(time.monotonic() - started) * 1000,
            detail=f"did not answer within {PROBE_TIMEOUT_SECONDS}s",
        )
    except Exception as exc:
        return DependencyStatus(
            name=name,
            ok=False,
            latency_ms=(time.monotonic() - started) * 1000,
            # The type, not the message: a driver's exception text can carry the DSN, and this
            # endpoint is reachable by whoever can reach the service.
            detail=type(exc).__name__,
        )
    return DependencyStatus(name=name, ok=True, latency_ms=(time.monotonic() - started) * 1000)


async def check_database(session: AsyncSession) -> DependencyStatus:
    async def probe() -> None:
        await session.execute(text("SELECT 1"))

    return await _probe("database", probe)


async def check_blob_store(store: BlobStore) -> DependencyStatus:
    async def probe() -> None:
        try:
            await store.get(_PROBE_KEY)
        except BlobNotFound:
            return  # the store answered, which is what we asked

    return await _probe("blob_store", probe)


async def readiness(session: AsyncSession, store: BlobStore) -> ReadinessReport:
    """Probe every dependency a request needs, concurrently.

    Concurrently because they are independent: run in sequence, a readiness check costs the sum
    of its timeouts, and an orchestrator polling every few seconds would spend most of a slow
    period waiting on probes it had already learned the answer to.
    """
    dependencies = list(await asyncio.gather(check_database(session), check_blob_store(store)))
    report = ReadinessReport(ready=all(d.ok for d in dependencies), dependencies=dependencies)
    if not report.ready:
        log.warning("readiness.not_ready", failing=[d.name for d in dependencies if not d.ok])
    return report
