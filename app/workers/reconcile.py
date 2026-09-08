"""One-shot ingestion reconciliation — ``uv run poe reconcile-ingestion``.

The worker already sweeps on a timer (``ingest_reconcile_interval_seconds``). This is the
operator's version of the same sweep, for when the worker has been down long enough that a
backlog of stranded sources has built up and you want it collected now rather than on the next
tick — or to see what a sweep *would* collect without waiting.
"""

import asyncio
import logging

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.services import ingestion
from app.workers.tasks import _enqueue_ingestion


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    async with SessionFactory() as session:
        report = await ingestion.reconcile_stranded(session, _enqueue_ingestion, settings=settings)
    print(f"requeued={report.requeued} abandoned={report.abandoned}")


if __name__ == "__main__":
    asyncio.run(main())
