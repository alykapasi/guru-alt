"""One answer, several components, marked separately (S10).

`record_observation` applied the item's single score to every tagged KC, varying only the
*weight* — which scales how far the estimate moves, never which way. So a learner who set a
least-squares problem up correctly and then botched the projection had that failure counted
against every skill the question touched, including the ones they had just demonstrated. And
nothing in the system had ever written a `Rubric` row, so `Item.rubric_id` was always null and
every open answer was graded against "(no explicit rubric; grade on correctness and
completeness)".
"""

import json
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation, mastery
from app.learning.mastery import Observation
from app.learning.rubric_grading import GradedComponent, grade_open
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef, Usage, text_of
from app.models.assessment import Item, ItemKC, ItemType, Rubric
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as svc


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"c-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _kcs(session: AsyncSession, names: list[str]) -> list[KC]:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kcs = [
        KC(
            topic_id=topic.id,
            slug=f"{n.lower()}-{uuid.uuid4().hex[:4]}",
            name=n,
            description=f"{n} desc",
        )
        for n in names
    ]
    session.add_all(kcs)
    await session.flush()
    return kcs


# --- the estimate ------------------------------------------------------------


async def test_a_failed_component_does_not_condemn_the_ones_that_passed(
    db_session: AsyncSession,
) -> None:
    """The tracker's own example. Before this, both KCs moved the same way."""
    learner = await _learner(db_session)
    setup, projection = await _kcs(db_session, ["Setup", "Projection"])

    states = await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={setup.id: 1.0, projection.id: 1.0},
            score=0.5,
            kc_scores={setup.id: 1.0, projection.id: 0.0},
        ),
    )
    by_kc = {s.kc_id: s for s in states}
    assert by_kc[setup.id].ability > 0.0
    assert by_kc[projection.id].ability < 0.0


async def test_without_component_scores_nothing_changes(db_session: AsyncSession) -> None:
    # An MCQ has one outcome and cannot say more. The aggregate still lands on every KC.
    learner = await _learner(db_session)
    a, b = await _kcs(db_session, ["A", "B"])

    states = await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={a.id: 1.0, b.id: 1.0}, score=0.0),
    )
    assert all(s.ability < 0.0 for s in states)


def by_kc_due(states, kc_id):
    """The KC's next review date, asserted present rather than narrowed inline."""
    due = next(s.due_at for s in states if s.kc_id == kc_id)
    assert due is not None
    return due


async def test_each_component_is_scheduled_on_its_own_result(db_session: AsyncSession) -> None:
    """Retention scheduling is per-KC state, so it has to follow the per-KC result: the
    component they demonstrated should not come back as soon as the one they failed."""
    learner = await _learner(db_session)
    strong, weak = await _kcs(db_session, ["Strong", "Weak"])

    states = await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={strong.id: 1.0, weak.id: 1.0},
            score=0.5,
            kc_scores={strong.id: 1.0, weak.id: 0.0},
        ),
    )
    strong_due = by_kc_due(states, strong.id)
    weak_due = by_kc_due(states, weak.id)
    assert strong_due > weak_due


async def test_the_event_records_both_the_component_and_the_item_score(
    db_session: AsyncSession,
) -> None:
    """`score` is the per-KC number because every consumer of an observation asks a per-KC
    question; the item's own score is kept so the answer can still be reassembled."""
    learner = await _learner(db_session)
    a, b = await _kcs(db_session, ["A", "B"])
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={a.id: 1.0, b.id: 1.0},
            score=0.5,
            kc_scores={a.id: 0.9, b.id: 0.1},
        ),
    )
    events = (await db_session.scalars(select(LearningEvent))).all()
    by_kc = {e.kc_id: e.payload for e in events}
    assert by_kc[a.id]["score"] == pytest.approx(0.9)
    assert by_kc[b.id]["score"] == pytest.approx(0.1)
    assert by_kc[a.id]["item_score"] == pytest.approx(0.5)
    assert by_kc[b.id]["item_score"] == pytest.approx(0.5)


async def test_a_component_score_for_an_untagged_kc_is_refused(db_session: AsyncSession) -> None:
    # Silently ignoring it would be the quiet kind of wrong: the grader believed it was
    # marking something this answer covered.
    learner = await _learner(db_session)
    (a,) = await _kcs(db_session, ["A"])
    with pytest.raises(ValueError, match="does not assess"):
        Observation(
            learner_id=learner.id,
            kc_weights={a.id: 1.0},
            score=0.5,
            kc_scores={a.id: 0.5, uuid.uuid4(): 0.5},
        )


# --- the grader --------------------------------------------------------------


class _RecordingProvider(FakeProvider):
    def __init__(self, reply: str, systems: list[str | None], prompts: list[str]) -> None:
        super().__init__(reply=reply)
        self._systems = systems
        self._prompts = prompts

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        self._systems.append(system)
        self._prompts.append(" ".join(text_of(m.content) for m in messages))
        return ChatResponse(
            content=self._reply,
            usage=Usage(input_tokens=1, output_tokens=1),
            model=model,
        )


def _recording(reply: str) -> tuple[LLMClient, list[str | None], list[str]]:
    systems: list[str | None] = []
    prompts: list[str] = []
    provider = _RecordingProvider(reply, systems, prompts)
    return (
        LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}),
        systems,
        prompts,
    )


def _components(n: int) -> list[GradedComponent]:
    return [GradedComponent(kc_id=uuid.uuid4(), name=f"KC{i}") for i in range(1, n + 1)]


async def test_the_grader_returns_a_mark_per_component() -> None:
    comps = _components(2)
    reply = json.dumps(
        {
            "components": [{"n": 1, "score": 1.0}, {"n": 2, "score": 0.0}],
            "score": 0.5,
            "rationale": "setup right, projection wrong",
        }
    )
    client, _, _ = _recording(reply)
    result, _usage = await grade_open(
        client, stem="q", response={"text": "a"}, rubric=None, components=comps
    )
    assert result.component_scores == {comps[0].kc_id: 1.0, comps[1].kc_id: 0.0}
    assert result.score == pytest.approx(0.5)


async def test_the_components_are_named_in_the_prompt() -> None:
    comps = [
        GradedComponent(kc_id=uuid.uuid4(), name="Projection", description="onto a subspace"),
        GradedComponent(kc_id=uuid.uuid4(), name="Normal equations"),
    ]
    client, systems, prompts = _recording(json.dumps({"score": 0.5}))
    await grade_open(client, stem="q", response={"text": "a"}, rubric=None, components=comps)
    assert "Projection" in prompts[0] and "onto a subspace" in prompts[0]
    assert "Normal equations" in prompts[0]
    assert systems[0] is not None and "component" in systems[0]


async def test_a_single_component_item_takes_the_cheaper_prompt() -> None:
    # One component needs no breakdown — the aggregate already is that component's score.
    client, systems, _ = _recording(json.dumps({"score": 0.7}))
    result, _ = await grade_open(
        client, stem="q", response={"text": "a"}, rubric=None, components=_components(1)
    )
    assert result.component_scores == {}
    assert systems[0] is not None and "numbered knowledge components" not in systems[0]


async def test_a_mangled_component_list_costs_only_the_breakdown() -> None:
    """Asymmetric with the aggregate on purpose: a missing overall score makes the grade
    unusable, while a bad component list just falls back to how this always worked."""
    client, _, _ = _recording(json.dumps({"components": "not a list", "score": 0.4}))
    result, _ = await grade_open(
        client, stem="q", response={"text": "a"}, rubric=None, components=_components(2)
    )
    assert result.component_scores == {}
    assert result.score == pytest.approx(0.4)


async def test_a_component_number_outside_the_list_is_dropped() -> None:
    comps = _components(2)
    reply = json.dumps(
        {"components": [{"n": 1, "score": 1.0}, {"n": 7, "score": 0.0}], "score": 1.0}
    )
    client, _, _ = _recording(reply)
    result, _ = await grade_open(
        client, stem="q", response={"text": "a"}, rubric=None, components=comps
    )
    assert result.component_scores == {comps[0].kc_id: 1.0}


async def test_a_rubric_is_only_shown_for_the_component_it_was_written_for(
    db_session: AsyncSession,
) -> None:
    """`Rubric.kc_id` names one KC. Handing its criteria to every component of a multi-KC
    item tells the grader to mark two other components against a third one's standard."""
    a, b = await _kcs(db_session, ["A", "B"])
    rubric = Rubric(kc_id=b.id, criteria={"criteria": ["mentions the residual"]})
    db_session.add(rubric)
    await db_session.flush()
    row = Item(item_type=ItemType.SHORT, stem="q", rubric_id=rubric.id)
    db_session.add(row)
    await db_session.flush()
    db_session.add_all([ItemKC(item_id=row.id, kc_id=a.id), ItemKC(item_id=row.id, kc_id=b.id)])
    await db_session.flush()
    # Through the service's own loader: it is what eager-loads kc_links and rubric, and the
    # grading path never sees an item that arrived any other way.
    item = await svc.get_item(db_session, row.id)
    assert item is not None

    components = await svc._components_of(db_session, item)
    by_kc = {c.kc_id: c for c in components}
    assert by_kc[b.id].criteria == {"criteria": ["mentions the residual"]}
    assert by_kc[a.id].criteria is None


# --- generated open questions carry criteria ---------------------------------


async def test_a_generated_open_question_brings_its_marking_criteria(
    db_session: AsyncSession,
) -> None:
    """Nothing produced a Rubric row before this, so every open answer was graded to whatever
    standard the grader improvised on the day."""
    (kc,) = await _kcs(db_session, ["Photosynthesis"])
    reply = json.dumps(
        {"stem": "Explain photosynthesis.", "criteria": ["names chlorophyll", "mentions light"]}
    )
    item, _ = await item_generation.generate_short_item(db_session, fake_llm_client(reply), kc)
    assert item is not None
    assert item.rubric_id is not None
    rubric = await db_session.get(Rubric, item.rubric_id)
    assert rubric is not None
    assert rubric.kc_id == kc.id
    assert rubric.criteria == {"criteria": ["names chlorophyll", "mentions light"]}


async def test_the_generator_is_asked_for_criteria(db_session: AsyncSession) -> None:
    (kc,) = await _kcs(db_session, ["X"])
    client, systems, _ = _recording(json.dumps({"stem": "q", "criteria": ["a"]}))
    await item_generation.generate_short_item(db_session, client, kc)
    assert systems[0] is not None and "criteria" in systems[0]


async def test_a_question_without_usable_criteria_is_still_a_question(
    db_session: AsyncSession,
) -> None:
    (kc,) = await _kcs(db_session, ["X"])
    item, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(json.dumps({"stem": "Explain X."})), kc
    )
    assert item is not None
    assert item.rubric_id is None


async def test_a_reply_with_no_stem_still_yields_nothing(db_session: AsyncSession) -> None:
    (kc,) = await _kcs(db_session, ["X"])
    item, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(json.dumps({"criteria": ["a"]})), kc
    )
    assert item is None


# --- end to end, including the idempotent replay ------------------------------


async def _answer_short(
    session: AsyncSession, learner: Learner, kcs: list[KC], reply: str, attempt_id=None
):
    row = Item(item_type=ItemType.SHORT, stem="Solve the least-squares problem.")
    session.add(row)
    await session.flush()
    session.add_all([ItemKC(item_id=row.id, kc_id=kc.id) for kc in kcs])
    await session.flush()
    item = await svc.get_item(session, row.id)
    assert item is not None
    client, _, _ = _recording(reply)
    return await svc.answer_item(
        session,
        learner.id,
        item,
        AnswerSubmit(response={"text": "my answer"}, attempt_id=attempt_id),
        llm=client,
    )


async def test_answering_a_multi_component_item_splits_the_evidence(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    kcs = await _kcs(db_session, ["Setup", "Projection"])
    reply = json.dumps(
        {"components": [{"n": 1, "score": 1.0}, {"n": 2, "score": 0.0}], "score": 0.5}
    )
    result, states = await _answer_short(db_session, learner, kcs, reply)

    assert result.component_scores[kcs[0].id] == pytest.approx(1.0)
    assert result.component_scores[kcs[1].id] == pytest.approx(0.0)
    by_kc = {s.kc_id: s for s in states}
    assert by_kc[kcs[0].id].ability > by_kc[kcs[1].id].ability


async def test_a_replayed_attempt_returns_the_item_score_not_a_component_s(
    db_session: AsyncSession,
) -> None:
    """`score` in the event payload is now this row's component score, so replaying the
    aggregate from it would report one component's mark as the whole answer's."""
    learner = await _learner(db_session)
    kcs = await _kcs(db_session, ["Setup", "Projection"])
    attempt = uuid.uuid4()
    reply = json.dumps(
        {"components": [{"n": 1, "score": 1.0}, {"n": 2, "score": 0.0}], "score": 0.5}
    )
    first, _ = await _answer_short(db_session, learner, kcs, reply, attempt_id=attempt)
    row = await db_session.scalar(select(Item).where(Item.stem.like("Solve%")))
    assert row is not None
    item = await svc.get_item(db_session, row.id)
    assert item is not None
    replayed, _ = await svc.answer_item(
        db_session,
        learner.id,
        item,
        AnswerSubmit(response={"text": "my answer"}, attempt_id=attempt),
        llm=fake_llm_client(json.dumps({"score": 0.0})),
    )
    assert replayed.score == pytest.approx(first.score) == pytest.approx(0.5)
    assert replayed.component_scores == first.component_scores


async def test_a_replay_does_not_invent_a_breakdown_the_grade_never_had(
    db_session: AsyncSession,
) -> None:
    """A single-component item has no per-component resolution, and the replay must not
    manufacture one from the fan-out — the retry has to return what the first request did."""
    learner = await _learner(db_session)
    kcs = await _kcs(db_session, ["Only"])
    attempt = uuid.uuid4()
    first, _ = await _answer_short(
        db_session, learner, kcs, json.dumps({"score": 0.8}), attempt_id=attempt
    )
    assert first.component_scores == {}

    row = await db_session.scalar(select(Item).where(Item.stem.like("Solve%")))
    assert row is not None
    item = await svc.get_item(db_session, row.id)
    assert item is not None
    replayed, _ = await svc.answer_item(
        db_session,
        learner.id,
        item,
        AnswerSubmit(response={"text": "my answer"}, attempt_id=attempt),
        llm=fake_llm_client(json.dumps({"score": 0.0})),
    )
    assert replayed.component_scores == {}
    assert replayed.score == pytest.approx(0.8)
