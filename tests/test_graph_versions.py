"""A deploy that changes a graph drops what it can no longer resume (S17).

A checkpoint is the state of one graph shape. Resume it under another — a renamed node, a new
state key — and it runs the wrong node or raises mid-turn. Each graph carries a version, every
checkpoint is stamped with it, and a checkpoint stamped with another (or none, or unreadable)
is discarded and the learner falls back cleanly.
"""

import hashlib
import json
import uuid
from typing import Any, get_type_hints

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.agent.refinement import (
    REFINEMENT_GRAPH_VERSION,
    RefinementState,
    build_refinement_graph,
    refinement_config,
)
from app.agent.workflow import (
    WORKFLOW_GRAPH_VERSION,
    WorkflowState,
    build_workflow_graph,
    workflow_config,
)
from app.api.deps import get_llm_client
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole, Usage
from app.main import app
from app.models.chat import OnboardingSession
from app.models.learner import Learner
from app.services import checkpoints, onboarding
from app.services import refinement as refinement_svc
from app.services import workflow as workflow_svc
from tests.test_workflow import PRESENT, _conversation_with_active_step, _drain

API = "/api/v1"
PROPOSAL = "Learn the basics of photosynthesis in two weeks."


# --- the stamp --------------------------------------------------------------------------------


async def test_a_paused_question_carries_its_graph_version(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
    await _drain(db_session, llm, conv, user_content="let's practice")

    graph = build_workflow_graph(llm, db_session, learner_id=conv.learner_id)
    snapshot = await graph.aget_state(workflow_config(str(conv.id)))
    assert checkpoints.compatible(snapshot, WORKFLOW_GRAPH_VERSION)


# --- practice ---------------------------------------------------------------------------------


async def test_a_paused_question_from_another_graph_version_is_dropped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
    await _drain(db_session, llm, conv, user_content="let's practice")
    assert await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)

    monkeypatch.setattr(workflow_svc, "WORKFLOW_GRAPH_VERSION", WORKFLOW_GRAPH_VERSION + 1)
    assert (
        await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)
        is None
    )
    graph = build_workflow_graph(llm, db_session, learner_id=conv.learner_id)
    assert (await graph.aget_state(workflow_config(str(conv.id)))).values == {}


async def test_an_unstamped_paused_question_is_dropped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything paused before this change shipped: dropped once, on first resume."""
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
    monkeypatch.setattr(
        workflow_svc, "workflow_config", lambda t: {"configurable": {"thread_id": t}}
    )
    await _drain(db_session, llm, conv, user_content="let's practice")
    monkeypatch.undo()

    assert (
        await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)
        is None
    )


async def test_an_unreadable_checkpoint_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    discarded: list[str] = []

    async def record(thread_id: str) -> bool:
        discarded.append(thread_id)
        return True

    class _Broken:
        async def aget_state(self, _config: Any) -> Any:
            raise ValueError("cannot deserialize")

    monkeypatch.setattr(checkpointing, "discard_thread", record)
    snapshot = await checkpoints.paused_state(
        _Broken(),
        workflow_config("t-broken"),
        graph_name="workflow",
        version=WORKFLOW_GRAPH_VERSION,
    )
    assert snapshot is None
    assert discarded == ["t-broken"]


# --- refinement -------------------------------------------------------------------------------


async def test_a_negotiation_from_another_graph_version_is_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = fake_llm_client(script=[FakeTurn(text=PROPOSAL)])
    conversation_id = uuid.uuid4()
    graph = build_refinement_graph(llm)
    await graph.ainvoke(
        {
            "messages": [ChatMessage(role=ChatRole.USER, content="photosynthesis")],
            "system": "s",
            "max_tokens": 100,
            "max_rounds": 3,
            "proposal": "",
            "usage": Usage(),
            "rounds": 0,
            "satisfied": False,
            "auto_committed": False,
            "agreed_goal": "",
        },
        refinement_config(str(conversation_id)),
    )
    assert await refinement_svc.is_awaiting_reply(llm, conversation_id) is True

    monkeypatch.setattr(refinement_svc, "REFINEMENT_GRAPH_VERSION", REFINEMENT_GRAPH_VERSION + 1)
    assert await refinement_svc.is_awaiting_reply(llm, conversation_id) is False


# --- onboarding -------------------------------------------------------------------------------


@pytest.fixture
def onboarding_llm() -> Any:
    client = fake_llm_client(script=[FakeTurn(text=PROPOSAL)])
    app.dependency_overrides[get_llm_client] = lambda: client
    yield client
    app.dependency_overrides.pop(get_llm_client, None)


def _sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_an_onboarding_session_from_another_graph_version_expires(
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
    onboarding_llm: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = (await api_client.post(f"{API}/onboarding/goal-sessions")).json()["session_id"]
    r = await api_client.post(
        f"{API}/onboarding/goal-turns",
        json={"session_id": session_id, "content": "photosynthesis", "mode": "start"},
    )
    assert any(e["type"] == "awaiting_reply" for e in _sse(r.text))

    monkeypatch.setattr(onboarding, "REFINEMENT_GRAPH_VERSION", REFINEMENT_GRAPH_VERSION + 1)
    r = await api_client.post(
        f"{API}/onboarding/goal-turns",
        json={"session_id": session_id, "content": "yes", "mode": "resume", "satisfied": True},
    )
    errors = [e for e in _sse(r.text) if e["type"] == "error"]
    assert errors and errors[0]["detail"] == onboarding.EXPIRED_DETAIL
    gone = await db_session.scalar(
        select(OnboardingSession).where(OnboardingSession.session_id == session_id)
    )
    assert gone is None


# --- the bump guard ---------------------------------------------------------------------------


def _fingerprint(compiled: Any, state: type) -> str:
    g = compiled.get_graph()
    shape = {
        "nodes": sorted(g.nodes),
        "edges": sorted([e.source, e.target, bool(e.conditional)] for e in g.edges),
        "state": sorted(get_type_hints(state)),
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]


PINNED = {
    "workflow": (1, "980c9d6b8ca529aa"),
    "refinement": (1, "36fd942e189dc05d"),
}


def test_a_graph_shape_change_bumps_its_version() -> None:
    llm = fake_llm_client()
    current = {
        "workflow": (
            WORKFLOW_GRAPH_VERSION,
            _fingerprint(
                build_workflow_graph(llm, AsyncSession(), learner_id=uuid.uuid4()), WorkflowState
            ),
        ),
        "refinement": (
            REFINEMENT_GRAPH_VERSION,
            _fingerprint(build_refinement_graph(llm), RefinementState),
        ),
    }
    for name, pinned in PINNED.items():
        assert current[name] == pinned, (
            f"the {name} graph changed shape: bump {name.upper()}_GRAPH_VERSION and update "
            f"PINNED[{name!r}] to {current[name]}"
        )
