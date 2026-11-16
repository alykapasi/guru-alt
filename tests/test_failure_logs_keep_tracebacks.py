"""A swallowed failure is logged with its traceback (review, 2026-11-16).

Best-effort bookkeeping catches everything so a turn survives it, which makes the log line the
only trace a failure leaves. ``error=str(exc)`` alone said what failed but not where.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.models.chat import ConversationPhase
from app.services.chat import record_phase


class _Broken(AsyncSession):
    async def get(self, *args, **kwargs):
        raise RuntimeError("database went away")


async def test_a_phase_that_fails_to_record_is_logged_with_its_traceback() -> None:
    with capture_logs() as logs:
        await record_phase(_Broken(), uuid.uuid4(), ConversationPhase.CHATTING)
    (entry,) = [e for e in logs if e["event"] == "chat.phase_not_recorded"]
    assert entry.get("exc_info") is True
