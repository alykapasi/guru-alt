"""Paused practice across a real restart, and the races durability made reachable (S17).

``test_durable_state`` resumes through a second saver in the same process; here the second
process is real. The races are the ones a durable, shared checkpoint opened: a second answer
while one is being graded, and a conversation deleted under a turn.
"""

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent import checkpointing
from app.api.deps import get_llm_client
from app.core.config import get_settings
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services import removal, retention, turn_lock
from app.services import workflow as workflow_svc
from app.storage.memory import InMemoryBlobStore
from tests.checkpoint_helpers import has_checkpoint
from tests.test_workflow import (
    PRESENT,
    RESPOND_2,
    RIGHT_GRADE,
    _conversation_with_active_step,
    _drain,
    _learner_and_subject_with_active_step,
    _parse_sse,
)

API = "/api/v1"


async def test_a_second_process_resumes_what_this_one_paused(
    engine: AsyncEngine, durable_checkpointer: None
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        conv = await _conversation_with_active_step(session)  # committed for real
        llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
        await _drain(session, llm, conv, user_content="let's practice")
        item_id = await workflow_svc.paused_item_id(
            llm, session, conv.id, learner_id=conv.learner_id
        )
        assert item_id is not None
        subject_id = conv.subject_id
    await checkpointing.stop()  # this process is gone
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.restart_probe",
            str(conv.id),
            str(conv.learner_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GURU_DATABASE_URL": get_settings().database_url},
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        assert proc.returncode == 0, err.decode()[-2000:]
        report = json.loads(out.decode().strip().splitlines()[-1])
        assert report["durable"] is True
        assert report["paused_item_id"] == str(item_id)
        assert "done" in report["events"]
    finally:
        await checkpointing.start(get_settings())
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await retention.delete_learner(session, InMemoryBlobStore(), conv.learner_id)
            await session.execute(delete(Subject).where(Subject.id == subject_id))
            await session.commit()


@pytest.fixture
def practice_llm() -> Iterator[None]:
    client = fake_llm_client(
        script=[
            FakeTurn(text=PRESENT),
            FakeTurn(text='{"intent": "attempt"}'),
            FakeTurn(text=RIGHT_GRADE),
            FakeTurn(text=RESPOND_2),
        ]
    )
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def test_a_second_answer_while_one_is_being_graded_is_refused(
    api_client: AsyncClient,
    db_session: AsyncSession,
    engine: AsyncEngine,
    api_learner: Learner,
    practice_llm: None,
) -> None:
    """Pins the guarantee S17's durability depends on: one paused question, one grade."""
    _learner, subject = await _learner_and_subject_with_active_step(db_session, learner=api_learner)
    r = await api_client.post(f"{API}/conversations", json={"subject_id": str(subject.id)})
    conversation_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "let's practice", "mode": "workflow"},
    )
    assert any(e["type"] == "awaiting_reply" for e in _parse_sse(r.text))

    in_flight = await turn_lock.claim(engine, uuid.UUID(conversation_id))
    assert in_flight is not None
    try:
        r = await api_client.post(
            f"{API}/conversations/{conversation_id}/messages",
            json={"content": "sunlight -> sugars"},
        )
        assert r.status_code == 409
        graded = await db_session.scalar(
            select(func.count())
            .select_from(LearningEvent)
            .where(LearningEvent.learner_id == api_learner.id)
        )
        assert graded == 0
    finally:
        await in_flight.release()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "sunlight -> sugars"},
    )
    assert any(e["type"] == "done" for e in _parse_sse(r.text))


async def test_deleting_a_conversation_mid_answer_ends_the_turn_cleanly(
    db_session: AsyncSession, durable_checkpointer: None
) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(
        script=[FakeTurn(text=PRESENT), FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND_2)]
    )
    await _drain(db_session, llm, conv, user_content="let's practice")

    stream = workflow_svc.run_workflow_turn(
        db_session,
        llm,
        learner_id=conv.learner_id,
        conversation=conv,
        user_content="sunlight -> sugars",
        max_tokens=300,
        max_rounds=3,
        resume=True,
    )
    events = [await anext(stream)]  # graded, and the feedback has started streaming
    await removal.delete_conversation(db_session, conv.learner_id, conv.id, forget=False)
    events += [e async for e in stream]

    assert events[-1].type == "error"
    assert events[-1].detail == workflow_svc.DELETED_DETAIL
    assert not await has_checkpoint(str(conv.id))
