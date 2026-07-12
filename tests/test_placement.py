"""Placement diagnostic: service + HTTP level."""

import json
import uuid
from collections.abc import Iterator, Sequence

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.learning.grading import auto_grade
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef, Usage, text_of
from app.main import app
from app.models.assessment import ItemType
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as assessment_svc
from app.services import placement as svc

API = "/api/v1"


class _SequencedProvider(FakeProvider):
    """Each call returns the next scripted reply (repeats the last once exhausted)."""

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
        tools: Sequence[ToolDef] | None = None,
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


MCQ_REPLY = json.dumps({"stem": "What is X?", "choices": ["A", "B", "C", "D"], "correct": 2})


def _inference_reply(kc_index: int, level: str = "strong") -> str:
    return json.dumps({"levels": [{"kc": kc_index, "level": level, "confidence": 0.9}]})


async def _graph(session: AsyncSession) -> tuple[Learner, Subject, KC, KC]:
    """A subject with a root KC and a dependent KC (root -> dependent prerequisite)."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    root = KC(topic_id=topic.id, slug="a-root", name="A Root")
    dependent = KC(topic_id=topic.id, slug="b-dependent", name="B Dependent")
    session.add_all([root, dependent])
    await session.flush()
    session.add(KCEdge(kc_id=dependent.id, prereq_kc_id=root.id))
    await session.flush()
    return learner, subject, root, dependent


# --- service layer ------------------------------------------------------


async def test_run_placement_seeds_inferred_kc_and_light_tests_root(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, dependent = await _graph(db_session)
    # Candidates ordered (topic.slug, kc.slug): [a-root=1, b-dependent=2].
    llm = _sequenced_client([_inference_reply(kc_index=2, level="strong"), MCQ_REPLY])

    result = await svc.run_placement(
        db_session,
        llm,
        learner_id=learner.id,
        subject=subject,
        background="I've done a lot of cell biology before.",
        light_test_size=3,
    )

    assert len(result.seeded) == 1
    assert result.seeded[0].kc_id == dependent.id
    assert result.seeded[0].ability == 1.75
    assert result.seeded[0].uncertainty == 0.6

    assert len(result.light_test_items) == 1
    item = result.light_test_items[0]
    assert item.item_type == ItemType.MCQ
    assert [link.kc_id for link in item.kc_links] == [root.id]


async def test_run_placement_reuses_existing_item_for_kc(db_session: AsyncSession) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    existing, _ = await svc.item_generation.generate_mcq_item(
        db_session, _sequenced_client([MCQ_REPLY]), root
    )
    assert existing is not None

    llm = _sequenced_client([_inference_reply(kc_index=2)])  # only inference call expected
    result = await svc.run_placement(
        db_session,
        llm,
        learner_id=learner.id,
        subject=subject,
        background="hi",
        light_test_size=3,
    )

    assert [i.id for i in result.light_test_items] == [existing.id]


async def test_run_placement_never_overwrites_existing_state(db_session: AsyncSession) -> None:
    learner, subject, _root, dependent = await _graph(db_session)
    llm = _sequenced_client([_inference_reply(kc_index=2, level="strong"), MCQ_REPLY])
    first = await svc.run_placement(
        db_session,
        llm,
        learner_id=learner.id,
        subject=subject,
        background="bg",
        light_test_size=3,
    )
    assert first.seeded[0].ability == 1.75

    # Re-run with a different inferred level for the same KC.
    llm2 = _sequenced_client([_inference_reply(kc_index=2, level="some"), MCQ_REPLY])
    second = await svc.run_placement(
        db_session,
        llm2,
        learner_id=learner.id,
        subject=subject,
        background="bg2",
        light_test_size=3,
    )
    assert second.seeded == []  # no-op: dependent already has state

    state = await db_session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == dependent.id
        )
    )
    assert state is not None
    assert state.ability == 1.75  # untouched by the second run


async def test_run_placement_light_test_answer_does_not_get_clobbered_by_rerun(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    llm = _sequenced_client([_inference_reply(kc_index=2, level="strong"), MCQ_REPLY])
    result = await svc.run_placement(
        db_session,
        llm,
        learner_id=learner.id,
        subject=subject,
        background="bg",
        light_test_size=3,
    )
    item = result.light_test_items[0]
    assert item.answer_key is not None
    correct_choice = item.answer_key["correct"]

    grade = auto_grade(ItemType.MCQ, item.answer_key, {"choice": correct_choice})
    assert grade.correct is True
    loaded = await assessment_svc.get_item(db_session, item.id)
    assert loaded is not None
    _, states = await assessment_svc.answer_item(
        db_session,
        learner.id,
        loaded,
        AnswerSubmit(response={"choice": correct_choice}),
        llm=llm,
    )
    real_ability = states[0].ability
    assert real_ability > 0.0  # correct answer raised it above the unseen prior

    # Re-running placement must not touch root's now-real evidence.
    llm2 = _sequenced_client([_inference_reply(kc_index=1, level="strong")])
    await svc.run_placement(
        db_session,
        llm2,
        learner_id=learner.id,
        subject=subject,
        background="bg2",
        light_test_size=3,
    )
    state = await db_session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == root.id
        )
    )
    assert state is not None
    assert state.ability == real_ability


async def test_run_placement_empty_subject_is_a_noop(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Empty")
    db_session.add_all([learner, subject])
    await db_session.flush()

    result = await svc.run_placement(
        db_session,
        _sequenced_client(["irrelevant"]),
        learner_id=learner.id,
        subject=subject,
        background="bg",
        light_test_size=3,
    )
    assert result.seeded == []
    assert result.light_test_items == []


# --- HTTP level -----------------------------------------------------------


@pytest.fixture
def sequenced_llm() -> Iterator[list[str]]:
    """Yields a mutable replies list the test fills in, applied via dependency override."""
    replies: list[str] = []
    app.dependency_overrides[get_llm_client] = lambda: _sequenced_client(replies)
    yield replies
    app.dependency_overrides.pop(get_llm_client, None)


async def test_placement_prompt_endpoint(api_client: AsyncClient, db_session: AsyncSession) -> None:
    r = await api_client.post(f"{API}/subjects", json={"slug": "chem", "name": "Chemistry"})
    subject_id = r.json()["id"]

    r = await api_client.get(f"{API}/subjects/{subject_id}/placement/prompt")
    assert r.status_code == 200
    assert "Chemistry" in r.json()["question"]

    r = await api_client.get(f"{API}/subjects/{uuid.uuid4()}/placement/prompt")
    assert r.status_code == 404


async def test_placement_endpoint_seeds_and_returns_items(
    api_client: AsyncClient, db_session: AsyncSession, sequenced_llm: list[str]
) -> None:
    r = await api_client.post(f"{API}/subjects", json={"slug": "phys", "name": "Physics"})
    subject_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/subjects/{subject_id}/topics", json={"slug": "t", "name": "T"}
    )
    topic_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs", json={"slug": "a-root", "name": "A Root"}
    )
    root_id = r.json()["id"]

    sequenced_llm.append(_inference_reply(kc_index=1, level="strong"))
    sequenced_llm.append(MCQ_REPLY)

    r = await api_client.post(
        f"{API}/subjects/{subject_id}/placement", json={"background": "I know this well"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["seeded"]) == 1
    assert body["seeded"][0]["kc_id"] == root_id
    assert len(body["light_test_items"]) == 1
    assert "answer_key" not in body["light_test_items"][0]

    r = await api_client.post(f"{API}/subjects/{uuid.uuid4()}/placement", json={"background": "x"})
    assert r.status_code == 404
