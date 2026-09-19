"""Administrator transcript activity remains visible without becoming learner evidence."""

import uuid
from unittest.mock import AsyncMock

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.chat import Conversation
from app.models.learner import Learner
from app.services import memory, profile, turn_common


async def test_attribution_survives_history_and_excludes_inference(
    db_session: AsyncSession,
    api_client: AsyncClient,
    api_learner: Learner,
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.flush()
    learner_message = await turn_common.add_message(db_session, conversation.id, "user", "My goal")
    actor, action = uuid.uuid4(), uuid.uuid4()
    db_session.info.update(admin_actor_id=str(actor), admin_action_id=str(action))
    admin_message = await turn_common.add_message(db_session, conversation.id, "user", "Admin goal")
    reply = await turn_common.add_message(db_session, conversation.id, "assistant", "Admin reply")
    db_session.info.pop("admin_actor_id")
    db_session.info.pop("admin_action_id")
    await db_session.commit()
    assert learner_message.admin_actor_id is None
    assert admin_message.admin_actor_id == reply.admin_actor_id == actor
    assert admin_message.admin_action_id == reply.admin_action_id == action
    assert [m.id for m in await profile._load_own_messages(db_session, api_learner.id)] == [
        learner_message.id
    ]
    window = await memory._unprocessed_messages(db_session, conversation.id, after=None, limit=20)
    assert [m.id for m in window] == [learner_message.id]
    response = await api_client.get(f"/api/v1/conversations/{conversation.id}/messages")
    assert response.status_code == 200
    rows = {m["id"]: m for m in response.json()["messages"]}
    assert rows[str(admin_message.id)]["admin_actor_id"] == str(actor)
    assert rows[str(reply.id)]["admin_action_id"] == str(action)


async def test_admin_only_history_never_calls_memory_extraction(
    db_session: AsyncSession,
    api_learner: Learner,
    monkeypatch,
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.info["admin_actor_id"] = str(uuid.uuid4())
    await turn_common.add_message(db_session, conversation.id, "user", "I prefer admin things")
    await turn_common.add_message(db_session, conversation.id, "assistant", "Acknowledged")
    db_session.info.pop("admin_actor_id")
    await db_session.commit()
    extract = AsyncMock()
    monkeypatch.setattr(memory, "extract_memories", extract)
    assert (
        await memory.write_back(db_session, fake_llm_client(), conversation_id=conversation.id)
        == []
    )
    extract.assert_not_awaited()
