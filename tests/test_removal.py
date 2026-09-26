"""Archive, delete and forget (S61, S42; V11)."""

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind
from app.services import memory as memory_svc
from tests.embedding import FAKE_SPACE


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _memory(
    session: AsyncSession, learner: Learner, conversation: Conversation | None, content: str
) -> Memory:
    embedding = (await fake_llm_client().embed(ModelRole.EMBED, [content])).vectors[0]
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner.id,
        conversation_id=conversation.id if conversation else None,
        origin_conversation_id=conversation.id if conversation else None,
        kind=MemoryKind.FACT,
        content=content,
        embedding=embedding,
    )
    session.add(memory)
    await session.flush()
    return memory


async def test_write_back_records_where_a_memory_came_from(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(
        Message(conversation_id=conversation.id, role="user", content="I study in the mornings.")
    )
    await db_session.commit()
    reply = json.dumps({"memories": [{"kind": "preference", "content": "Studies in mornings"}]})

    created = await memory_svc.write_back(
        db_session, fake_llm_client(reply=reply), conversation_id=conversation.id
    )

    assert created and all(m.origin_conversation_id == conversation.id for m in created)
