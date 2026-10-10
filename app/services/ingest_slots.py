"""Exact ingestion slots, global and per learner (S37).

A claim used to count live jobs in a subquery of its own UPDATE — tighter than read-then-write,
but under READ COMMITTED two racing claims could both see room and both take it. A slot is a
Postgres session-level advisory lock on a connection held for the job, the ``turn_lock``
pattern: exact under any race, and freed by Postgres when a crashed worker's connection ends,
so it needs no timeout. A job takes one global slot of ``ingest_max_concurrent_jobs`` and one
of its learner's ``ingest_max_jobs_per_learner``, so one learner's pile of uploads cannot take
every slot.

Released with ``pg_advisory_unlock_all()`` before the connection goes back to the pool: the
pool reuses connections, and a session lock outlives ``close()`` on a pooled one.
"""

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings
from app.services.turn_lock import _as_int4

log = structlog.get_logger(__name__)

GLOBAL_NAMESPACE = 0x494E4753  # "INGS"
LEARNER_NAMESPACE = 0x494E474C  # "INGL"


def learner_key(learner_id: uuid.UUID, slot: int) -> int:
    """The learner's ``slot``-th lock id: 28 bits of the learner, 4 bits of slot (≤ 16)."""
    return ((learner_id.int & 0x0FFFFFFF) << 4) | slot


@dataclass
class IngestSlots:
    """A held pair of slots. :meth:`release` is idempotent."""

    connection: AsyncConnection
    _released: bool = field(default=False, repr=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self.connection.execute(text("SELECT pg_advisory_unlock_all()"))
        except Exception:
            log.warning("ingest_slots.unlock_failed")
        finally:
            await self.connection.close()


async def _try(connection: AsyncConnection, namespace: int, key: int) -> bool:
    return bool(
        await connection.scalar(
            text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": _as_int4(namespace), "objid": _as_int4(key)},
        )
    )


async def take(
    engine: AsyncEngine, learner_id: uuid.UUID, settings: Settings
) -> IngestSlots | None:
    """A global and a learner slot, or ``None`` when either kind is full."""
    connection = await engine.connect()
    connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
    slots = IngestSlots(connection)
    try:
        for i in range(settings.ingest_max_concurrent_jobs):
            if await _try(connection, GLOBAL_NAMESPACE, i):
                break
        else:
            await slots.release()
            return None
        for j in range(settings.ingest_max_jobs_per_learner):
            if await _try(connection, LEARNER_NAMESPACE, learner_key(learner_id, j)):
                return slots
        await slots.release()
        return None
    except BaseException:
        await slots.release()
        raise
