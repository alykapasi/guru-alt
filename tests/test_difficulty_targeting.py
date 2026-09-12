"""How hard the next question is now follows from what we believe about the learner (S12).

Two things were broken, and only the first was written down. Target difficulty was computed
from the profile, stored on the plan step, and interpolated into a tutor prompt as a raw
float — and never reached selection or generation, which the session runner said in its own
docstring. The quieter half: no generator had ever written a ``difficulty``, so every item in
the bank sat at the 0.0 column default. Targeting a bank of zeros is a no-op dressed as a
feature, so both halves are covered here — where the number comes from, and that items carry
one at all.
"""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import difficulty, item_generation, mastery
from app.learning.mastery import Observation
from app.learning.tracer import Estimate, GlickoEstimator
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef, Usage, text_of
from app.models.assessment import Item, ItemKC, ItemType
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.services import assessment as assessment_svc
from app.services import learner_context
from app.services import lesson_plan as lesson_plan_svc
from app.services import placement as placement_svc
from app.services import session_runner as runner_svc
from app.services.lesson_plan import PlanGroundingContext

MCQ_REPLY = json.dumps({"stem": "What is X?", "choices": ["A", "B", "C", "D"], "correct": 2})
SHORT_REPLY = json.dumps({"stem": "Explain X in your own words."})
T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


# --- the pure targeting math ------------------------------------------------


@pytest.mark.parametrize("ability", [-2.0, -0.4, 0.0, 1.25, 2.0])
@pytest.mark.parametrize("rate", [0.3, 0.5, 0.75, 0.85])
def test_a_targeted_item_is_one_the_learner_succeeds_at_that_often(
    ability: float, rate: float
) -> None:
    """The whole claim, stated as a round trip: ask for the difficulty at which this learner
    succeeds ``rate`` of the time, and the estimator agrees that they do.

    The grid stays inside the clamp on purpose — every combination here must round-trip
    exactly, and a widened clamp is what makes that true across the whole useful scale."""
    estimate = Estimate(ability=ability, uncertainty=0.4)
    target = difficulty.target_for(estimate, success_rate=rate)
    assert GlickoEstimator().expected(estimate, difficulty=target) == pytest.approx(rate)


def test_past_the_clamp_the_target_quietly_under_delivers() -> None:
    """What the bound costs, stated rather than hidden: ask for a success rate that would need
    an item off the scale and you get the edge of the scale, which is harder than you asked
    for. Worth knowing before anyone reads a realised rate as evidence about the learner."""
    estimate = Estimate(ability=-6.0)
    target = difficulty.target_for(estimate, success_rate=0.75)
    assert target == difficulty.DIFFICULTY_FLOOR
    assert GlickoEstimator().expected(estimate, difficulty=target) < 0.75


def test_the_informative_target_is_the_learner_s_own_ability() -> None:
    """Where ``E * (1 - E)`` peaks, which is what one answer moving the estimate most means.
    Not a coincidence worth leaving implicit: it is why placement and practice differ."""
    estimate = Estimate(ability=1.3, uncertainty=0.5)
    assert difficulty.target_for(
        estimate, success_rate=difficulty.INFORMATIVE_SUCCESS_RATE
    ) == pytest.approx(1.3)


def test_practice_asks_for_something_easier_than_assessment_would() -> None:
    estimate = Estimate(ability=0.5)
    practice = difficulty.target_for(estimate, success_rate=0.75)
    informative = difficulty.target_for(estimate, success_rate=0.5)
    assert practice < informative


def test_a_harder_learner_gets_a_harder_question() -> None:
    weak = difficulty.target_for(Estimate(ability=-1.0), success_rate=0.75)
    strong = difficulty.target_for(Estimate(ability=2.0), success_rate=0.75)
    assert weak < strong


def test_uncertainty_does_not_move_the_target() -> None:
    """Documented as deliberate, so it is pinned: an uncertainty-aware target would be a
    guessed interpolation, and S18 exists to calibrate guesses rather than bury them."""
    narrow = difficulty.target_for(Estimate(ability=0.5, uncertainty=0.05), success_rate=0.75)
    wide = difficulty.target_for(Estimate(ability=0.5, uncertainty=1.0), success_rate=0.75)
    assert narrow == wide


def test_a_runaway_estimate_cannot_request_an_item_off_the_scale() -> None:
    assert difficulty.target_for(Estimate(ability=40.0), success_rate=0.75) == (
        difficulty.DIFFICULTY_CEILING
    )
    assert difficulty.target_for(Estimate(ability=-40.0), success_rate=0.75) == (
        difficulty.DIFFICULTY_FLOOR
    )


@pytest.mark.parametrize("rate", [0.0, 1.0, -0.1, 1.5])
def test_an_impossible_success_rate_is_a_programming_error(rate: float) -> None:
    # Unlike a model's output, this can only come from our own code — so it raises rather
    # than degrading to some default that would silently mis-pitch every question.
    with pytest.raises(ValueError):
        difficulty.target_for(Estimate(), success_rate=rate)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-9.0, "introductory"),
        (-1.6, "introductory"),
        (-1.5, "straightforward"),
        (-0.6, "straightforward"),
        (-0.5, "moderate"),
        (0.49, "moderate"),
        (0.5, "challenging"),
        (1.49, "challenging"),
        (1.5, "demanding"),
        (9.0, "demanding"),
    ],
)
def test_bands_partition_the_scale_at_their_stated_edges(value: float, expected: str) -> None:
    assert difficulty.band(value) == expected


def test_describing_a_difficulty_gives_the_model_something_to_act_on() -> None:
    described = difficulty.describe(0.0)
    assert described.startswith("moderate")
    assert len(described) > len("moderate")  # the gloss, not just the label


# --- selection ---------------------------------------------------------------


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"d-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _kc(session: AsyncSession) -> tuple[Subject, KC]:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:6]}", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:6]}", name="K")
    session.add(kc)
    await session.flush()
    return subject, kc


async def _item(session: AsyncSession, kc: KC, stem: str, level: float) -> Item:
    item = Item(item_type=ItemType.MCQ, stem=stem, difficulty=level, answer_key={"correct": 0})
    session.add(item)
    await session.flush()
    session.add(ItemKC(item_id=item.id, kc_id=kc.id, weight=1.0))
    await session.flush()
    return item


async def _answer(
    session: AsyncSession, learner: Learner, kc: KC, item: Item, *, when: datetime
) -> None:
    await mastery.record_observation(
        session,
        Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0, item_id=item.id),
        now=when,
    )
    await session.flush()


async def test_selection_picks_the_item_nearest_the_target(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _, kc = await _kc(db_session)
    await _item(db_session, kc, "easy", -2.0)
    await _item(db_session, kc, "hard", 2.0)

    chosen = await assessment_svc.find_item_for_kc(
        db_session, kc.id, learner_id=learner.id, target_difficulty=1.7
    )
    assert chosen is not None and chosen.stem == "hard"


async def test_a_well_pitched_question_still_loses_to_a_fresh_one(
    db_session: AsyncSession,
) -> None:
    """S14's rule stays on top. A question at exactly the right level that the learner
    answered an hour ago measures memory of that question, not the component."""
    learner = await _learner(db_session)
    _, kc = await _kc(db_session)
    perfect = await _item(db_session, kc, "perfect fit", 1.0)
    await _item(db_session, kc, "wrong level", -2.5)
    await _answer(db_session, learner, kc, perfect, when=T0)

    chosen = await assessment_svc.find_item_for_kc(
        db_session, kc.id, learner_id=learner.id, target_difficulty=1.0
    )
    assert chosen is not None and chosen.stem == "wrong level"


async def test_fit_breaks_ties_between_two_items_seen_equally_long_ago(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    _, kc = await _kc(db_session)
    near = await _item(db_session, kc, "near", 0.8)
    far = await _item(db_session, kc, "far", -2.5)
    # Answered at the same instant, so exposure cannot separate them and fit must.
    await _answer(db_session, learner, kc, far, when=T0)
    await _answer(db_session, learner, kc, near, when=T0)

    chosen = await assessment_svc.find_item_for_kc(
        db_session, kc.id, learner_id=learner.id, target_difficulty=1.0
    )
    assert chosen is not None and chosen.stem == "near"


async def test_without_a_target_selection_is_exactly_what_s14_left(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    _, kc = await _kc(db_session)
    first = await _item(db_session, kc, "first", 2.5)
    await _item(db_session, kc, "second", 0.0)

    # No target: creation order decides, so the badly pitched item still comes first.
    untargeted = await assessment_svc.find_item_for_kc(db_session, kc.id, learner_id=learner.id)
    assert untargeted is not None and untargeted.id == first.id


# --- generation --------------------------------------------------------------


class _RecordingProvider(FakeProvider):
    """Records the system prompt of every ``complete`` call."""

    def __init__(self, reply: str, systems: list[str | None]) -> None:
        super().__init__(reply=reply)
        self._systems = systems

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
        usage = Usage(
            input_tokens=sum(len(text_of(m.content).split()) for m in messages),
            output_tokens=len(self._reply.split()),
        )
        return ChatResponse(content=self._reply, usage=usage, model=model)


def _recording_client(reply: str) -> tuple[LLMClient, list[str | None]]:
    systems: list[str | None] = []
    provider = _RecordingProvider(reply, systems)
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})
    return client, systems


async def test_a_generated_item_records_the_level_it_was_asked_for(
    db_session: AsyncSession,
) -> None:
    _, kc = await _kc(db_session)
    item, _ = await item_generation.generate_mcq_item(
        db_session, fake_llm_client(MCQ_REPLY), kc, target_difficulty=1.25
    )
    assert item is not None
    assert item.difficulty == pytest.approx(1.25)


async def test_the_generator_is_told_a_band_and_never_the_number(
    db_session: AsyncSession,
) -> None:
    _, kc = await _kc(db_session)
    client, systems = _recording_client(MCQ_REPLY)
    await item_generation.generate_mcq_item(db_session, client, kc, target_difficulty=1.25)

    assert len(systems) == 1
    prompt = systems[0]
    assert prompt is not None
    assert "challenging" in prompt
    assert "1.25" not in prompt  # a logit is not an instruction a model can follow


@pytest.mark.parametrize(
    ("generate", "reply"),
    [
        (item_generation.generate_mcq_item, MCQ_REPLY),
        (item_generation.generate_short_item, SHORT_REPLY),
        (item_generation.generate_flashcard_item, json.dumps({"stem": "q", "answer": "a"})),
        (item_generation.generate_fill_blank_item, json.dumps({"stem": "x ___", "answer": "y"})),
    ],
)
async def test_every_generator_carries_the_target_through(
    db_session: AsyncSession, generate: item_generation.GeneratorFn, reply: str
) -> None:
    # All four are reachable from the session runner's type preference, so a generator that
    # quietly ignored the target would leave one item type uncalibrated and nothing else.
    _, kc = await _kc(db_session)
    client, systems = _recording_client(reply)
    item, _ = await generate(db_session, client, kc, target_difficulty=-1.75)
    assert item is not None
    assert item.difficulty == pytest.approx(-1.75)
    assert systems[0] is not None and "introductory" in systems[0]


async def test_an_untargeted_generator_says_nothing_about_level(
    db_session: AsyncSession,
) -> None:
    _, kc = await _kc(db_session)
    client, systems = _recording_client(MCQ_REPLY)
    item, _ = await item_generation.generate_mcq_item(db_session, client, kc)
    assert item is not None
    assert item.difficulty == 0.0
    assert systems[0] is not None and "Pitch" not in systems[0]


# --- the paths that resolve a target ----------------------------------------


async def _graph(session: AsyncSession) -> tuple[Learner, Subject, KC, KC]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
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


async def _next_item_difficulty(session: AsyncSession, ability: float) -> float:
    learner, subject, root, _ = await _graph(session)
    session.add(LearnerKCState(learner_id=learner.id, kc_id=root.id, ability=ability))
    await session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    item = await runner_svc.next_item(
        session, fake_llm_client(MCQ_REPLY), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    return item.difficulty


async def test_practice_pitches_at_what_the_tracer_believes(db_session: AsyncSession) -> None:
    ability = 1.5
    generated = await _next_item_difficulty(db_session, ability)
    expected = difficulty.target_for(
        Estimate(ability=ability), success_rate=get_settings().practice_target_success_rate
    )
    assert generated == pytest.approx(expected)


async def test_a_stronger_learner_is_given_a_harder_question(db_session: AsyncSession) -> None:
    """The tracker's desired result in one line: estimated capability changes the task."""
    weak = await _next_item_difficulty(db_session, -1.0)
    strong = await _next_item_difficulty(db_session, 2.0)
    assert weak < strong


async def test_guided_practice_pitches_itself(db_session: AsyncSession) -> None:
    # The workflow passes no target, so the runner has to resolve one or guided practice
    # silently opts out of every question being pitched at all.
    learner = await _learner(db_session)
    _, kc = await _kc(db_session)
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=kc.id, ability=2.0))
    await db_session.flush()

    item = await runner_svc.short_answer_item_for_kc(
        db_session, fake_llm_client(SHORT_REPLY), learner_id=learner.id, kc=kc
    )
    assert item is not None
    assert item.difficulty == pytest.approx(
        difficulty.target_for(
            Estimate(ability=2.0), success_rate=get_settings().practice_target_success_rate
        )
    )


async def test_a_due_review_is_pitched_from_the_ability_it_already_carries(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    _, kc = await _kc(db_session)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=kc.id,
            ability=1.0,
            due_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.flush()

    resolved = await runner_svc.due_review_items(
        db_session,
        fake_llm_client(json.dumps({"stem": "q", "answer": "a"})),
        learner_id=learner.id,
        item_limit=1,
    )
    assert len(resolved) == 1
    _, item = resolved[0]
    assert item is not None
    assert item.difficulty == pytest.approx(
        difficulty.target_for(
            Estimate(ability=1.0), success_rate=get_settings().practice_target_success_rate
        )
    )


# --- placement wants the opposite ------------------------------------------


def _inference_reply(kc_index: int, level: str) -> str:
    return json.dumps({"levels": [{"kc": kc_index, "level": level, "confidence": 0.9}]})


class _SequencedProvider(FakeProvider):
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


async def _light_test_difficulty(session: AsyncSession, level: str | None) -> float:
    learner, subject, _root, _ = await _graph(session)
    inference = _inference_reply(1, level) if level else json.dumps({"levels": []})
    result = await placement_svc.run_placement(
        session,
        _sequenced_client([inference, MCQ_REPLY]),
        learner_id=learner.id,
        subject=subject,
        background="some background",
        light_test_size=1,
    )
    assert len(result.light_test_items) == 1
    return result.light_test_items[0].difficulty


async def test_the_light_test_asks_what_it_cannot_predict(db_session: AsyncSession) -> None:
    """A diagnostic wants information, not comfort. Practice would shift this target down by
    a logit to make success likely; placement must not — and the exact value is the only thing
    that tells the two intents apart, since both move with the learner."""
    strong = placement_svc._ESTIMATE_BY_LEVEL["strong"]
    pitched = await _light_test_difficulty(db_session, "strong")
    assert pitched == pytest.approx(
        difficulty.target_for(strong, success_rate=difficulty.INFORMATIVE_SUCCESS_RATE)
    )
    assert pitched != pytest.approx(
        difficulty.target_for(strong, success_rate=get_settings().practice_target_success_rate)
    )


async def test_claiming_a_strong_background_raises_the_light_test(
    db_session: AsyncSession,
) -> None:
    told_nothing = await _light_test_difficulty(db_session, None)
    told_strong = await _light_test_difficulty(db_session, "strong")
    assert told_strong > told_nothing


# --- what the tutor is told -------------------------------------------------


def test_the_tutor_is_told_a_level_not_a_float() -> None:
    """This prompt used to carry "Target difficulty: 0.00." — and 0.00 was the only value it
    could ever take, because nothing had written a difficulty to an item. An instruction a
    model cannot act on is not inert; it still steers the turn."""
    note = learner_context.plan_note(
        PlanGroundingContext(
            subject_name="Linear Algebra",
            kc_id=uuid.uuid4(),
            kc_name="Vector spaces",
            step_type="new",
            target_difficulty=1.7,
            hint_density=None,
            preferred_item_type=None,
        )
    )
    assert note is not None
    assert "demanding" in note
    assert "1.7" not in note


def test_a_plan_with_no_target_says_nothing_about_level() -> None:
    note = learner_context.plan_note(
        PlanGroundingContext(
            subject_name="Linear Algebra",
            kc_id=uuid.uuid4(),
            kc_name="Vector spaces",
            step_type="new",
            target_difficulty=None,
            hint_density=None,
            preferred_item_type=None,
        )
    )
    assert note is not None and "level" not in note
