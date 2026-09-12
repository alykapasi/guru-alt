"""What a conversation is waiting for is recorded, not inferred (S52).

The frontend used to reconstruct this from "no goal committed and the last message is from the
assistant". That is true of a refinement-gate goal proposal — and equally true of an agentic
reply given before any goal was committed, so a tool-using answer was presented to the learner
with accept/refine buttons under it. The backend always knew which flow ran; these tests hold
it to saying so, and to still saying so after a reload.
"""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE, get_llm_client
from app.api.v1.chat import TurnFlow, _phase_after
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import Conversation, ConversationPhase
from app.models.learner import Learner

API = "/api/v1"


@pytest.fixture
def fake_llm() -> Iterator[None]:
    client = fake_llm_client(script=[FakeTurn(text="Here is an answer.")] * 6)
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def _dev_conversation(session: AsyncSession, api_client: AsyncClient) -> str:
    learner = await session.scalar(
        Learner.__table__.select().where(Learner.handle == DEV_LEARNER_HANDLE)
    )
    if learner is None:
        session.add(Learner(handle=DEV_LEARNER_HANDLE))
        await session.commit()
    r = await api_client.post(f"{API}/conversations", json={})
    return r.json()["id"]


def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


# --- the rule itself ---------------------------------------------------------------------


def test_a_gate_turn_that_asked_something_leaves_a_proposal() -> None:
    assert (
        _phase_after(
            TurnFlow.REFINEMENT, awaiting_reply=True, workflow_paused=False, check_open=False
        )
        is ConversationPhase.GOAL_PROPOSED
    )


def test_the_same_signal_from_the_workflow_means_a_practice_item() -> None:
    """`awaiting_reply` is emitted by both flows, for entirely different things."""
    assert (
        _phase_after(TurnFlow.WORKFLOW, awaiting_reply=True, workflow_paused=True, check_open=False)
        is ConversationPhase.AWAITING_ANSWER
    )


def test_a_reply_that_asked_nothing_leaves_nothing_pending() -> None:
    """The exact case the frontend's old inference called a goal proposal."""
    for flow in TurnFlow:
        assert (
            _phase_after(flow, awaiting_reply=False, workflow_paused=False, check_open=False)
            is ConversationPhase.CHATTING
        )


def test_a_tutor_or_agentic_turn_never_proposes_a_goal() -> None:
    assert (
        _phase_after(TurnFlow.AGENTIC, awaiting_reply=True, workflow_paused=False, check_open=False)
        is ConversationPhase.CHATTING
    )
    assert (
        _phase_after(TurnFlow.TUTOR, awaiting_reply=True, workflow_paused=False, check_open=False)
        is ConversationPhase.CHATTING
    )


# --- end to end through the router --------------------------------------------------------


async def test_a_gate_turn_records_a_pending_proposal(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    conversation_id = await _dev_conversation(db_session, api_client)

    await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "I want to learn photosynthesis", "mode": "chat"},
    )

    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    await db_session.refresh(conversation)
    assert conversation.phase == ConversationPhase.GOAL_PROPOSED


async def test_an_agentic_turn_on_a_goalless_conversation_is_not_a_proposal(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    """The defect, end to end: no goal, assistant spoke last, and still not a proposal."""
    conversation_id = await _dev_conversation(db_session, api_client)

    await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "search my notes for mitochondria", "mode": "agentic"},
    )

    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    await db_session.refresh(conversation)
    assert conversation.goal is None  # the precondition the old inference keyed on
    assert conversation.phase == ConversationPhase.CHATTING


async def test_the_phase_is_exposed_so_a_reload_can_read_it(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    conversation_id = await _dev_conversation(db_session, api_client)
    await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "search my notes", "mode": "agentic"},
    )

    listed = (await api_client.get(f"{API}/conversations")).json()
    row = next(c for c in listed if c["id"] == conversation_id)

    assert row["phase"] == ConversationPhase.CHATTING
    assert row["active_item_id"] is None


async def test_a_new_conversation_starts_with_nothing_pending(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    conversation_id = await _dev_conversation(db_session, api_client)

    listed = (await api_client.get(f"{API}/conversations")).json()
    row = next(c for c in listed if c["id"] == conversation_id)

    assert row["phase"] == ConversationPhase.CHATTING


def test_an_agentic_interjection_does_not_unpause_a_practice_item() -> None:
    """`mode="agentic"` is checked before a paused workflow, so it steps around the item
    rather than answering it — and the very next message resumes that item."""
    assert (
        _phase_after(TurnFlow.AGENTIC, awaiting_reply=False, workflow_paused=True, check_open=False)
        is ConversationPhase.AWAITING_ANSWER
    )
