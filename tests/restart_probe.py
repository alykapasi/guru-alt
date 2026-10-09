"""A second process for ``tests/test_restart.py``: resume a practice another process paused.

``python -m tests.restart_probe <conversation_id> <learner_id>`` prints one JSON line: whether
the saver was durable, the paused item it found, and the event types of resuming it.
"""

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.agent import checkpointing
from app.core.db import SessionFactory
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation
from app.services import workflow
from app.services.llm_log import set_accounting_session_factory

RIGHT_GRADE = '{"score": 0.9, "rationale": "much better"}'
RESPOND = "Great job, you've got it!"


class _Discarded:
    def add(self, obj: object) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def execute(self, *args: object, **kwargs: object) -> None:
        pass


@asynccontextmanager
async def _discard() -> AsyncIterator[Any]:
    yield _Discarded()


async def _main(conversation_id: uuid.UUID, learner_id: uuid.UUID) -> dict[str, Any]:
    set_accounting_session_factory(_discard)
    await checkpointing.start()
    try:
        if not checkpointing.is_durable():
            return {"durable": False}
        llm = fake_llm_client(script=[FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND)])
        async with SessionFactory() as session:
            conversation = await session.get(Conversation, conversation_id)
            assert conversation is not None
            item = await workflow.paused_item_id(
                llm, session, conversation_id, learner_id=learner_id
            )
            events = [
                e.type
                async for e in workflow.run_workflow_turn(
                    session,
                    llm,
                    learner_id=learner_id,
                    conversation=conversation,
                    user_content="sunlight -> sugars",
                    max_tokens=300,
                    max_rounds=3,
                    resume=True,
                )
            ]
        return {"durable": True, "paused_item_id": str(item) if item else None, "events": events}
    finally:
        await checkpointing.stop()


if __name__ == "__main__":
    report = asyncio.run(_main(uuid.UUID(sys.argv[1]), uuid.UUID(sys.argv[2])))
    print(json.dumps(report))
