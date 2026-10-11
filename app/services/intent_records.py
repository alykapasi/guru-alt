"""Keep the practice gate's intent decision where it can be sampled (S59).

The gate decides what a reply does about the open question — an attempt, a deferral, a
withdrawal — and until now kept nothing unless Jev was asking the same question. Evaluation
needs that decision to label it, so it is recorded here, for eval-eligible accounts only.

Written on its own session, like ``app.services.decision_log``: the record must survive the turn
rolling back, and a failed write is logged and swallowed — recording a decision must never cost
the learner their turn.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from app.learning.conversation_evidence import TurnIntent
from app.learning.turn_read import ReadContext
from app.models.evaluation import IntentRecord
from app.models.learner import Learner
from app.services.llm_log import accounting_session

log = structlog.get_logger(__name__)


async def record(context: ReadContext, *, stem: str, message: str, intent: TurnIntent) -> None:
    """Record ``intent`` if the learner is eval-eligible. Never raises."""
    if context.learner_id is None:
        return
    try:
        async with accounting_session() as session:
            eligible = await session.scalar(
                select(Learner.eval_eligible).where(Learner.id == context.learner_id)
            )
            if not eligible:
                return
            session.add(
                IntentRecord(
                    learner_id=context.learner_id,
                    conversation_id=context.conversation_id,
                    item_id=context.item_id,
                    stem=stem,
                    message=message,
                    intent=intent.value,
                )
            )
            await session.commit()
    except Exception as exc:
        log.warning("intent_record.failed", error=type(exc).__name__)
