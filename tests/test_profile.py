"""Learner profile: service + HTTP level (plumbing — dimension catalog tests live elsewhere)."""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.profile_estimators import DIMENSION_SPECS
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatMessage, ChatResponse, ModelRole, Usage, text_of
from app.models.assessment import Item, ItemType
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.profile import LearnerProfile, ProfileDimension
from app.services import profile as svc

API = "/api/v1"


class _SequencedProvider(FakeProvider):
    """Each call returns the next scripted reply (repeats the last once exhausted).

    ``refresh_profile`` makes several LLM calls in one run (error_type, goal_orientation,
    interests all share the FAST role) — ``fake_llm_client`` only scripts one canned reply,
    so a full-refresh integration test needs this (same pattern as ``test_placement.py``).
    """

    def __init__(self, replies: list[str]) -> None:
        super().__init__()
        self._replies = replies
        self._calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        reply = self._replies[min(self._calls, len(self._replies) - 1)]
        self._calls += 1
        usage = Usage(
            input_tokens=sum(len(text_of(m.content).split()) for m in messages),
            output_tokens=len(reply.split()),
        )
        return ChatResponse(content=reply, usage=usage, model=model)


def _sequenced_client(replies: list[str]) -> LLMClient:
    provider = _SequencedProvider(replies)
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


def _obs(
    learner_id: uuid.UUID,
    *,
    score: float,
    difficulty: float = 0.5,
    latency_ms: int = 4000,
    hints_used: int = 0,
    item_id: uuid.UUID | None = None,
    minutes_offset: int,
) -> LearningEvent:
    base = datetime(2026, 1, 1)  # naive — matches learning_events.created_at's column type
    return LearningEvent(
        learner_id=learner_id,
        event_type="observation",
        payload={
            "score": score,
            "difficulty": difficulty,
            "latency_ms": latency_ms,
            "hints_used": hints_used,
            "item_id": str(item_id) if item_id else None,
            "response": None,
        },
        created_at=base + timedelta(minutes=minutes_offset),
    )


# --- service layer ------------------------------------------------------


async def test_refresh_profile_bare_learner_is_a_noop(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    dims = await svc.refresh_profile(db_session, learner.id, fake_llm_client())
    assert dims == []
    profile = await db_session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner.id)
    )
    assert profile is not None  # header row created even with nothing to show yet


async def test_get_snapshot_is_read_only(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="pace",
            value={"median_seconds": 12.0},
            uncertainty=0.5,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.commit()
    snapshot = await svc.get_snapshot(db_session, learner.id)
    assert [d.key for d in snapshot] == ["pace"]


async def test_reset_dimension_unknown_key_raises(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    with pytest.raises(KeyError):
        await svc.reset_dimension(db_session, learner.id, "not_a_real_dimension")


async def _rich_learner(session: AsyncSession) -> Learner:
    """A learner with enough history to trigger every dimension in the catalog at once."""
    learner = await _learner(session)
    mcq_a = Item(item_type=ItemType.MCQ, stem="A", answer_key={"choices": ["x"], "correct": 0})
    mcq_b = Item(item_type=ItemType.MCQ, stem="B", answer_key={"choices": ["x"], "correct": 0})
    cloze_c = Item(item_type=ItemType.CLOZE, stem="C", answer_key={"blanks": ["x"]})
    session.add_all([mcq_a, mcq_b, cloze_c])
    await session.flush()

    # Session 1: a mixed run across two MCQ items and a cloze item — enough for pace,
    # help_seeking, error_type, persistence, cognitive_load_tolerance, format_effectiveness.
    session1 = [
        _obs(
            learner.id, score=0.0, item_id=mcq_a.id, hints_used=1, latency_ms=5000, minutes_offset=0
        ),
        _obs(
            learner.id, score=1.0, item_id=mcq_a.id, hints_used=0, latency_ms=4000, minutes_offset=3
        ),
        _obs(
            learner.id, score=0.0, item_id=mcq_b.id, hints_used=2, latency_ms=6000, minutes_offset=6
        ),
        _obs(
            learner.id, score=0.0, item_id=mcq_b.id, hints_used=2, latency_ms=6000, minutes_offset=9
        ),
        _obs(
            learner.id,
            score=1.0,
            item_id=cloze_c.id,
            hints_used=0,
            latency_ms=3000,
            minutes_offset=12,
        ),
        _obs(
            learner.id,
            score=1.0,
            item_id=cloze_c.id,
            hints_used=0,
            latency_ms=3000,
            minutes_offset=15,
        ),
        _obs(
            learner.id,
            score=1.0,
            item_id=cloze_c.id,
            hints_used=0,
            latency_ms=3000,
            minutes_offset=18,
        ),
    ]
    # Session 2 (>30 min later): a clean, all-correct run — session_logistics needs a second
    # session, and engagement reads only the most recent one.
    session2 = [
        _obs(learner.id, score=1.0, latency_ms=3000, minutes_offset=2000 + i * 5) for i in range(3)
    ]
    session.add_all(session1 + session2)

    conversation = Conversation(
        learner_id=learner.id,
        goal="I want to deeply understand chemistry, not just pass a test.",
    )
    session.add(conversation)
    await session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            content=(
                "I really enjoy playing basketball and video games in my free time. I also "
                "like cooking pasta dishes and watching science documentaries about space "
                "and astronomy whenever I get a chance to relax after school."
            ),
        )
    )
    await session.flush()
    return learner


async def test_refresh_profile_rich_learner_triggers_every_dimension(
    db_session: AsyncSession,
) -> None:
    learner = await _rich_learner(db_session)
    error_type_reply = json.dumps(
        {
            "classifications": [
                {"item": 1, "type": "conceptual"},
                {"item": 2, "type": "procedural"},
                {"item": 3, "type": "conceptual"},
            ]
        }
    )
    goal_orientation_reply = json.dumps({"orientation": "mastery", "confidence": 0.9})
    interests_reply = json.dumps({"interests": ["basketball", "cooking"]})
    llm = _sequenced_client([error_type_reply, goal_orientation_reply, interests_reply])

    dims = await svc.refresh_profile(db_session, learner.id, llm)

    assert {d.key for d in dims} == {spec.key for spec in DIMENSION_SPECS}
    calls = (
        await db_session.scalars(select(LLMCall).where(LLMCall.learner_id == learner.id))
    ).all()
    assert len(calls) == 3  # error_type, goal_orientation, interests


async def test_reset_then_refresh_recomputes_a_dimension(db_session: AsyncSession) -> None:
    learner = await _rich_learner(db_session)
    llm = _sequenced_client(
        [
            json.dumps({"classifications": []}),
            json.dumps({"orientation": "mastery", "confidence": 0.5}),
            json.dumps({"interests": []}),
        ]
    )
    await svc.refresh_profile(db_session, learner.id, llm)

    removed = await svc.reset_dimension(db_session, learner.id, "pace")
    assert removed is True
    assert "pace" not in {d.key for d in await svc.get_snapshot(db_session, learner.id)}

    llm2 = _sequenced_client(
        [
            json.dumps({"classifications": []}),
            json.dumps({"orientation": "mastery", "confidence": 0.5}),
            json.dumps({"interests": []}),
        ]
    )
    dims = await svc.refresh_profile(db_session, learner.id, llm2)
    assert "pace" in {d.key for d in dims}


# --- HTTP level -----------------------------------------------------------


async def test_profile_endpoints_round_trip(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    r = await api_client.get(f"{API}/profile")
    assert r.status_code == 200
    assert r.json() == {"dimensions": []}

    r = await api_client.post(f"{API}/profile/refresh")
    assert r.status_code == 200
    assert r.json() == {"dimensions": []}

    r = await api_client.post(f"{API}/profile/not_a_real_dimension/reset")
    assert r.status_code == 404
