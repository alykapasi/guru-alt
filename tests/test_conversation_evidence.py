"""A conversation that produces evidence, and one that only looks like it (S15).

Mastery had exactly one door: the guided-practice workflow calling ``answer_item``. A learner
who explained an idea correctly in chat had demonstrated nothing the system recorded, and the
practice item a subject-scoped tutor turn resolved was handed to the client as a widget, never
mentioned to the tutor, and replaced by a different one on the next turn.

The two failure modes these tests hold apart are not symmetric. Recording nothing leaves the
question open and the learner able to answer it. Recording an invented attempt moves the
ability estimate, reschedules the FSRS card and revises the lesson plan — so every ambiguous
case here must land on "no evidence", and that is most of what is asserted below.
"""

import json
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.api.v1.chat import TurnFlow, _open_check_id, _phase_after
from app.learning import conversation_evidence, item_generation
from app.learning.conversation_evidence import TurnIntent, classify_intent, parse_intent
from app.learning.diagnosis import Diagnosis, FailureKind
from app.learning.grading import GradeResult
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatChunk, ChatMessage, ChatResponse, ModelRole, ToolDef, Usage
from app.main import app
from app.models.assessment import Item, ItemType
from app.models.chat import Conversation, ConversationPhase
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.schemas.assessment import ItemRead
from app.services import chat as chat_svc
from app.services import knowledge as knowledge_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services.chat import CheckOutcome, _check_note, _feedback_note
from app.services.turn_common import TurnEvent

SHORT_REPLY = json.dumps({"stem": "What is velocity?", "criteria": ["speed", "direction"]})
GRADE_REPLY = '{"score": 0.9, "rationale": "Both parts named."}'


class _RoleProvider(FakeProvider):
    """Answers each model role differently — the check gate is FAST, grading is SMART.

    Records every ``system`` prompt it is given, on both call shapes: the gate and the grader
    use ``complete``, the tutor's reply streams, and tests here assert about all three.
    """

    def __init__(self, by_role: dict[str, str]) -> None:
        super().__init__(reply="")
        self._by_role = by_role
        self.systems: list[str | None] = []
        self.streamed_systems: list[str | None] = []

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        self.systems.append(system)
        return ChatResponse(
            content=self._by_role.get(model, ""),
            model=model,
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        self.streamed_systems.append(system)
        yield ChatChunk(text=self._by_role.get(model, "reply"))
        yield ChatChunk(usage=Usage(input_tokens=1, output_tokens=1))


def _role_client(*, fast: str, smart: str) -> tuple[LLMClient, _RoleProvider]:
    provider = _RoleProvider({"m-fast": fast, "m-smart": smart})
    specs = {r: ModelSpec("fake", "m-smart") for r in ModelRole}
    specs[ModelRole.FAST] = ModelSpec("fake", "m-fast")
    return LLMClient({"fake": provider}, specs), provider


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"ce-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _kc(session: AsyncSession) -> KC:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="Motion")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:4]}", name="Velocity", description="d")
    session.add(kc)
    await session.flush()
    return kc


async def _open_check(session: AsyncSession) -> tuple[Learner, Conversation, Item]:
    """A conversation with a posed, unanswered check — the state every gate test starts from."""
    learner = await _learner(session)
    kc = await _kc(session)
    item, _ = await item_generation.generate_short_item(session, fake_llm_client(SHORT_REPLY), kc)
    assert item is not None
    conversation = Conversation(
        learner_id=learner.id,
        phase=ConversationPhase.AWAITING_ANSWER,
        active_item_id=item.id,
    )
    session.add(conversation)
    await session.commit()
    return learner, conversation, item


async def _events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.learner_id == learner_id)
            )
        ).all()
    )


# --- the gate's vocabulary ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"intent": "attempt"}', TurnIntent.ATTEMPT),
        ('{"intent": "deferral"}', TurnIntent.DEFERRAL),
        ('{"intent": "withdrawal"}', TurnIntent.WITHDRAWAL),
        ('  {"intent": "ATTEMPT"}  ', TurnIntent.ATTEMPT),
        ('Sure! {"intent": "attempt"} hope that helps', TurnIntent.ATTEMPT),
    ],
)
def test_a_readable_intent_is_read(raw: str, expected: TurnIntent) -> None:
    assert parse_intent(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "attempt",
        "{",
        "{not json}",
        "[]",
        '"attempt"',
        '{"intent": null}',
        '{"intent": "guess"}',
        '{"intent": 1}',
        '{"other": "attempt"}',
        "null",
    ],
)
def test_anything_unreadable_records_no_evidence(raw: str) -> None:
    """Every unparseable reply must land on DEFERRAL, never ATTEMPT.

    This is the asymmetry the module exists to enforce: a deferral leaves the question open,
    and a wrongly-inferred attempt writes a score to the tracer for an answer nobody gave.
    """
    assert parse_intent(raw) is TurnIntent.DEFERRAL


async def test_an_empty_message_is_not_an_attempt_and_costs_nothing() -> None:
    client, provider = _role_client(fast='{"intent": "attempt"}', smart="")
    intent, usage = await classify_intent(client, question="What is velocity?", message="   ")
    assert intent is TurnIntent.DEFERRAL
    assert usage.input_tokens == 0
    assert provider.systems == []  # no model call at all


async def test_a_provider_failure_is_a_deferral_not_a_zero() -> None:
    class _Broken(FakeProvider):
        async def complete(self, **kwargs: object) -> ChatResponse:
            raise RuntimeError("provider down")

    client = LLMClient({"fake": _Broken(reply="")}, {r: ModelSpec("fake", "m") for r in ModelRole})
    intent, _ = await classify_intent(client, question="q", message="an actual answer")
    assert intent is TurnIntent.DEFERRAL


# --- what a check does to the conversation ------------------------------------


async def test_an_attempt_at_an_open_check_becomes_mastery_evidence(
    db_session: AsyncSession,
) -> None:
    """The gap S15 names: this is the first path to the tracer that is not answer_item's
    endpoint or the guided-practice workflow."""
    learner, conversation, item = await _open_check(db_session)
    client, _ = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)

    open_check, outcome = await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Velocity is speed with a direction.",
    )

    assert open_check is None  # answered, so no longer in play
    assert outcome is not None
    assert outcome.item.id == item.id
    assert outcome.result.score == pytest.approx(0.9)
    events = await _events(db_session, learner.id)
    assert [e.event_type for e in events] == ["observation"]


async def test_a_question_back_records_nothing_and_keeps_the_check(
    db_session: AsyncSession,
) -> None:
    learner, conversation, item = await _open_check(db_session)
    client, _ = _role_client(fast='{"intent": "deferral"}', smart=GRADE_REPLY)

    open_check, outcome = await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Wait, what do you mean by direction?",
    )

    assert open_check is not None and open_check.id == item.id
    assert outcome is None
    assert await _events(db_session, learner.id) == []


async def test_declining_a_question_is_not_failing_it(db_session: AsyncSession) -> None:
    """A learner must be able to move on without the refusal being recorded as evidence they
    could not do it. The check is dropped; the tracer hears nothing."""
    learner, conversation, _item = await _open_check(db_session)
    client, _ = _role_client(fast='{"intent": "withdrawal"}', smart=GRADE_REPLY)

    open_check, outcome = await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Actually, let's talk about something else.",
    )

    assert open_check is None and outcome is None
    assert await _events(db_session, learner.id) == []


async def test_a_conversation_with_no_check_open_produces_no_evidence(
    db_session: AsyncSession,
) -> None:
    """Ordinary chat stays ordinary chat. Nothing is graded when nothing was asked."""
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id, phase=ConversationPhase.CHATTING)
    db_session.add(conversation)
    await db_session.commit()
    client, provider = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)

    open_check, outcome = await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="The derivative of x squared is 2x.",
    )

    assert (open_check, outcome) == (None, None)
    assert provider.systems == []  # not even the cheap gate call
    assert await _events(db_session, learner.id) == []


async def test_a_stale_item_pointer_without_the_phase_is_not_a_question(
    db_session: AsyncSession,
) -> None:
    """Both signals are required, and ``record_phase`` is best-effort — a bookkeeping write
    that fails can leave the pointer behind after the phase has moved on. Reading the pointer
    alone would then grade an ordinary message against a question that is no longer open."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    item, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), kc
    )
    assert item is not None
    conversation = Conversation(
        learner_id=learner.id, phase=ConversationPhase.CHATTING, active_item_id=item.id
    )
    db_session.add(conversation)
    await db_session.commit()

    client, provider = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)
    assert await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Speed with direction.",
    ) == (None, None)
    assert provider.systems == []
    assert await _events(db_session, learner.id) == []


async def test_a_check_whose_item_was_deleted_leaves_nothing_to_answer(
    db_session: AsyncSession,
) -> None:
    """``active_item_id`` is ``ON DELETE SET NULL``, so a deleted item clears the pointer
    rather than leaving the conversation holding a dangling one. The gate then sees no check,
    which is the truth: there is no longer a question to answer."""
    learner, conversation, item = await _open_check(db_session)
    await db_session.delete(item)
    await db_session.commit()
    await db_session.refresh(conversation)
    assert conversation.active_item_id is None

    client, provider = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)
    assert await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="an answer",
    ) == (None, None)
    assert provider.systems == []
    assert await _events(db_session, learner.id) == []


# --- posing the check ---------------------------------------------------------


async def _planned_subject(session: AsyncSession, learner_id: uuid.UUID) -> tuple[Subject, KC]:
    """A subject with a lesson plan, so a tutor turn has an active step to pose a check for."""
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="Motion")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:4]}", name="Velocity", description="d")
    session.add(kc)
    await session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        session, fake_llm_client(), learner_id=learner_id, subject_id=subject.id, goal=None
    )
    return subject, kc


async def _tutor_turn(
    session: AsyncSession, client: LLMClient, conversation: Conversation
) -> list[object]:
    return [
        ev
        async for ev in chat_svc.run_tutor_turn(
            session,
            client,
            learner_id=conversation.learner_id,
            conversation=conversation,
            history=[],
            user_content="tell me about motion",
            max_tokens=64,
        )
    ]


async def test_a_posed_check_is_marked_so_the_router_records_it(
    db_session: AsyncSession,
) -> None:
    """The item on a done event is only the conversation's open question when it says so. An
    unmarked item is informational — shown, not asked — and recording that one as active would
    have the learner's next message graded against a question nobody put to them."""
    learner = await _learner(db_session)
    subject, kc = await _planned_subject(db_session, learner.id)
    item, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), kc
    )
    assert item is not None
    conversation = Conversation(learner_id=learner.id, subject_id=subject.id, goal="learn")
    db_session.add(conversation)
    await db_session.commit()

    client, provider = _role_client(fast="", smart="A reply.")
    events = await _tutor_turn(db_session, client, conversation)

    done = next(e for e in events if e.type == "done")  # ty: ignore[unresolved-attribute]
    assert done.detail == "check"  # ty: ignore[unresolved-attribute]
    assert done.item is not None and done.item.id == item.id  # ty: ignore[unresolved-attribute]
    # and the tutor was actually told about it, so its prose asks the same question the
    # client is showing rather than inventing a second one alongside it.
    (system,) = provider.streamed_systems
    assert system is not None and item.stem in system


async def test_a_general_conversation_is_grounded_but_never_checked(
    db_session: AsyncSession,
) -> None:
    """A subject-less conversation still gets plan grounding, from whichever plan the learner
    was last on. It does not get a check: grounding aimed at the wrong subject costs an odd
    paragraph, while answering a check writes a mastery observation — and one recorded against
    a KC chosen by a cross-subject heuristic is evidence about a skill this conversation may
    never have touched."""
    learner = await _learner(db_session)
    _subject, kc = await _planned_subject(db_session, learner.id)
    item, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), kc
    )
    assert item is not None
    conversation = Conversation(learner_id=learner.id, subject_id=None, goal="learn")
    db_session.add(conversation)
    await db_session.commit()

    client, provider = _role_client(fast="", smart="A reply.")
    events = await _tutor_turn(db_session, client, conversation)

    done = next(e for e in events if e.type == "done")  # ty: ignore[unresolved-attribute]
    assert done.item is None and done.detail == ""  # ty: ignore[unresolved-attribute]
    (system,) = provider.streamed_systems
    assert system is not None and "lesson-plan focus" in system  # grounded all the same


async def test_a_new_check_starts_with_no_help_recorded_against_it(
    db_session: AsyncSession,
) -> None:
    """Help is counted per question. Carrying the previous check's scaffolds forward would
    discount every later answer for explanations the learner was given about something else —
    and the count never falls, so the discount would only ever deepen."""
    learner = await _learner(db_session)
    subject, kc = await _planned_subject(db_session, learner.id)
    item, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), kc
    )
    assert item is not None
    conversation = Conversation(
        learner_id=learner.id, subject_id=subject.id, goal="learn", active_item_scaffolds=5
    )
    db_session.add(conversation)
    await db_session.commit()

    client, _ = _role_client(fast="", smart="A reply.")
    await _tutor_turn(db_session, client, conversation)

    await db_session.refresh(conversation)
    assert conversation.active_item_scaffolds == 0


async def test_an_open_check_is_not_replaced_by_a_fresh_one(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check the learner has not answered stays the check. Posing a new one each turn is
    what the old code did — and it means the question the learner is looking at and the one
    their answer is graded against drift apart within a single conversation."""
    learner = await _learner(db_session)
    subject, kc = await _planned_subject(db_session, learner.id)
    posed, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), kc
    )
    assert posed is not None
    conversation = Conversation(
        learner_id=learner.id,
        subject_id=subject.id,
        goal="learn",
        phase=ConversationPhase.AWAITING_ANSWER,
        active_item_id=posed.id,
    )
    db_session.add(conversation)
    await db_session.commit()

    async def _must_not_run(*args: object, **kwargs: object) -> Item:
        raise AssertionError("a fresh item was resolved while a check was still open")

    monkeypatch.setattr("app.services.session_runner.short_answer_item_for_kc", _must_not_run)
    client, _ = _role_client(fast='{"intent": "deferral"}', smart="A reply.")
    events = await _tutor_turn(db_session, client, conversation)

    done = next(e for e in events if e.type == "done")  # ty: ignore[unresolved-attribute]
    assert done.item is not None and done.item.id == posed.id  # ty: ignore[unresolved-attribute]
    assert done.detail == "check"  # ty: ignore[unresolved-attribute]


async def test_the_turn_that_grades_an_answer_does_not_immediately_ask_another(
    db_session: AsyncSession,
) -> None:
    """Feedback is the turn. Answering and being re-questioned in one breath gives the learner
    nowhere to put the correction."""
    learner = await _learner(db_session)
    subject, kc = await _planned_subject(db_session, learner.id)
    posed, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), kc
    )
    assert posed is not None
    conversation = Conversation(
        learner_id=learner.id,
        subject_id=subject.id,
        goal="learn",
        phase=ConversationPhase.AWAITING_ANSWER,
        active_item_id=posed.id,
    )
    db_session.add(conversation)
    await db_session.commit()

    client, provider = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)
    events = await _tutor_turn(db_session, client, conversation)

    done = next(e for e in events if e.type == "done")  # ty: ignore[unresolved-attribute]
    assert done.item is None  # ty: ignore[unresolved-attribute]
    assert done.detail == ""  # ty: ignore[unresolved-attribute]
    # and the grade did reach the tutor's prompt as instructions
    assert any(
        s is not None and "has just attempted that question" in s for s in provider.streamed_systems
    )


async def test_a_check_that_cannot_be_answered_in_prose_is_not_graded(
    db_session: AsyncSession,
) -> None:
    """Only SHORT items are posed as conversational checks, so this is a guard rather than a
    routine path — but the thing it guards against is silent. A prose answer to an MCQ is not
    a wrong answer, it is an ungradable one, and scoring it would write a zero to the tracer
    for a learner who may well have known it."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    mcq, _ = await item_generation.generate_mcq_item(
        db_session,
        fake_llm_client(
            json.dumps({"stem": "Which?", "choices": ["a", "b", "c", "d"], "correct": 1})
        ),
        kc,
    )
    assert mcq is not None
    conversation = Conversation(
        learner_id=learner.id,
        phase=ConversationPhase.AWAITING_ANSWER,
        active_item_id=mcq.id,
    )
    db_session.add(conversation)
    await db_session.commit()

    client, _ = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)
    open_check, outcome = await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="the second one",
    )

    assert open_check is not None and open_check.id == mcq.id  # still waiting
    assert outcome is None
    assert await _events(db_session, learner.id) == []


# --- help before the attempt --------------------------------------------------


async def test_each_deferral_counts_as_help_against_the_eventual_attempt(
    db_session: AsyncSession,
) -> None:
    """Asking for an explanation and then answering is not an independent demonstration."""
    learner, conversation, _item = await _open_check(db_session)
    deferring, _ = _role_client(fast='{"intent": "deferral"}', smart=GRADE_REPLY)

    for _ in range(2):
        await chat_svc._resolve_check(
            db_session,
            deferring,
            learner_id=learner.id,
            conversation=conversation,
            user_content="I don't follow — can you explain?",
        )

    assert conversation.active_item_scaffolds == 2


async def test_help_before_the_attempt_reaches_the_tracer_as_assistance(
    db_session: AsyncSession,
) -> None:
    """The scaffold count is not bookkeeping: it discounts the observation exactly as a
    guided-practice hint does (app.learning.assistance)."""
    learner, conversation, _item = await _open_check(db_session)
    conversation.active_item_scaffolds = 3
    await db_session.commit()
    client, _ = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)

    await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Speed with direction.",
    )

    (event,) = await _events(db_session, learner.id)
    assert event.payload["hints_used"] == 3


# --- what the tutor is told ---------------------------------------------------


def test_the_tutor_is_told_the_question_that_is_actually_on_the_table() -> None:
    """The tutor used to be told nothing about the item, so its prose asked one question while
    the client showed another — and only the one nobody answered was graded."""
    item = Item(item_type=ItemType.SHORT, stem="State the difference between speed and velocity.")
    note = _check_note(item)
    assert "State the difference between speed and velocity." in note
    assert "do not swap in a different question" in note


def _outcome(item: Item, result: GradeResult) -> CheckOutcome:
    """A graded check with no mastery movement attached.

    Every test here is about the *prompt* the grade produces, and the prior and posterior
    states only feed the learner-facing report (S15). Empty rather than fabricated: a made-up
    movement in a fixture is the kind of thing that later reads as a fact about the estimator.
    """
    return CheckOutcome(item=item, result=result, priors={}, states=[])


def test_a_diagnosis_becomes_an_instruction_about_what_to_teach() -> None:
    """S09 defined the vocabulary so code could branch on it. This is the branch: the same
    score produces different teaching depending on why the answer fell short."""
    kc_id = uuid.uuid4()
    item = Item(item_type=ItemType.SHORT, stem="q")

    conceptual = _feedback_note(
        _outcome(
            item,
            GradeResult(
                score=0.3,
                correct=False,
                detail={},
                diagnoses={kc_id: Diagnosis(kind=FailureKind.CONCEPTUAL, confidence=0.8)},
            ),
        ),
        {kc_id: "Velocity"},
    )
    procedural = _feedback_note(
        _outcome(
            item,
            GradeResult(
                score=0.3,
                correct=False,
                detail={},
                diagnoses={kc_id: Diagnosis(kind=FailureKind.PROCEDURAL, confidence=0.8)},
            ),
        ),
        {kc_id: "Velocity"},
    )

    assert "Re-teach the idea itself" in conceptual
    assert "do not offer more practice yet" in conceptual
    assert "Do not re-explain the idea" in procedural
    assert conceptual != procedural


def test_a_grade_with_nothing_diagnosed_asserts_no_reason() -> None:
    """An MCQ knows an answer was wrong and nothing about why, and the prompt must read that
    way — inventing a reason would have the tutor repair a misconception nobody diagnosed."""
    item = Item(item_type=ItemType.MCQ, stem="q")
    note = _feedback_note(
        _outcome(item, GradeResult(score=0.0, correct=False, detail={})),
        {},
    )
    assert "0.00" in note and "not correct" in note
    for kind in FailureKind:
        assert kind.value not in note


@pytest.mark.parametrize("kind", [FailureKind.NONE, FailureKind.INCOMPLETE])
def test_a_diagnosis_with_nothing_to_repair_produces_no_teaching_instruction(
    kind: FailureKind,
) -> None:
    """NONE means the component was fine and INCOMPLETE means nothing was demonstrated either
    way — a blank, an off-topic reply, an answer that stopped partway. Neither is a diagnosed
    misconception, and attaching a repair to either would have the tutor re-teach an idea on
    the strength of a learner having written nothing (S09)."""
    kc_id = uuid.uuid4()
    note = _feedback_note(
        _outcome(
            Item(item_type=ItemType.SHORT, stem="q"),
            GradeResult(
                score=0.0,
                correct=False,
                detail={},
                diagnoses={kc_id: Diagnosis(kind=kind, confidence=0.6)},
            ),
        ),
        {kc_id: "Velocity"},
    )
    assert "Re-teach" not in note
    assert "what went wrong was" not in note
    assert kind.value not in note


def test_an_unverified_quote_is_not_put_in_the_learners_mouth() -> None:
    """S09 records whether the evidence span was really found in the response. One that was
    not is still a usable diagnosis, but must not be shown back as the learner's own words."""
    kc_id = uuid.uuid4()
    item = Item(item_type=ItemType.SHORT, stem="q")

    def note(verbatim: bool) -> str:
        return _feedback_note(
            _outcome(
                item,
                GradeResult(
                    score=0.2,
                    correct=False,
                    detail={},
                    diagnoses={
                        kc_id: Diagnosis(
                            kind=FailureKind.NOTATION,
                            evidence="v is the same as s",
                            evidence_verbatim=verbatim,
                        )
                    },
                ),
            ),
            {kc_id: "Velocity"},
        )

    assert "They wrote:" in note(True)
    assert "They wrote:" not in note(False)
    assert "notation" in note(False)  # the diagnosis itself still lands


# --- the phase the conversation is left in ------------------------------------


def test_only_a_marked_check_becomes_the_question_the_next_message_answers() -> None:
    """The guided-practice workflow also puts an item on its ``done`` event — the one it has
    just finished grading. Treating any item as the open question would leave that
    conversation waiting for an answer to a question the learner already answered, and hand
    their next message to the grader for it."""
    item = ItemRead(
        id=uuid.uuid4(),
        item_type=ItemType.SHORT,
        stem="q",
        difficulty=0.0,
        rubric_id=None,
        kcs=[],
        presentation={},
        origin="generated",
    )
    posed = TurnEvent(type="done", detail="check", item=item)
    finished = TurnEvent(type="done", detail="mastered", item=item)
    mentioned = TurnEvent(type="done", detail="", item=item)
    nothing = TurnEvent(type="done", detail="check", item=None)

    assert _open_check_id(posed) == item.id
    assert _open_check_id(finished) is None
    assert _open_check_id(mentioned) is None
    assert _open_check_id(nothing) is None


def test_a_turn_that_leaves_a_question_says_it_is_waiting_for_an_answer() -> None:
    """Without this the learner's next message is read as more conversation, and the answer
    they just typed never reaches the grader."""
    assert (
        _phase_after(TurnFlow.TUTOR, awaiting_reply=False, workflow_paused=False, check_open=True)
        is ConversationPhase.AWAITING_ANSWER
    )
    assert (
        _phase_after(TurnFlow.TUTOR, awaiting_reply=False, workflow_paused=False, check_open=False)
        is ConversationPhase.CHATTING
    )


def test_the_gate_runs_on_the_cheap_tier() -> None:
    """Classification gates a SMART grading call, so it must not cost one."""
    assert conversation_evidence.CHECK_ROLE is ModelRole.FAST


# --- what the learner is told (S15) ------------------------------------------------------------


async def test_a_graded_answer_is_reported_back_with_the_mastery_it_moved(
    db_session: AsyncSession,
) -> None:
    """Before this, a conversational answer was graded, updated mastery, rescheduled the card
    and revised the plan — and the learner was told none of it. The tutor's reply was the only
    evidence anything had happened, and a reply is not a record."""
    learner, conversation, item = await _open_check(db_session)
    client, _ = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)

    _open, outcome = await chat_svc._resolve_check(
        db_session,
        client,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Velocity is speed with a direction.",
    )
    assert outcome is not None
    kcs = await knowledge_svc.get_kcs(db_session, [link.kc_id for link in outcome.item.kc_links])
    result = chat_svc._check_result(outcome, kcs)

    assert result.item_id == item.id
    assert result.score == pytest.approx(0.9)
    assert result.correct is True
    assert len(result.components) == len(item.kc_links)
    component = result.components[0]
    assert component.kc_name
    # The movement, not just the destination: a posterior on its own gives the learner no
    # baseline to read it against.
    assert component.prior_ability == pytest.approx(0.0)
    assert component.ability > component.prior_ability


def test_a_component_the_grader_could_not_score_separately_reports_no_score() -> None:
    """Copying the item's aggregate down would present one verdict as several measurements,
    which is the exact error S10 exists to stop."""
    kc = KC(id=uuid.uuid4(), topic_id=uuid.uuid4(), slug="v", name="Velocity")
    outcome = _outcome(
        Item(id=uuid.uuid4(), item_type=ItemType.MCQ, stem="q"),
        GradeResult(score=0.0, correct=False, detail={}),
    )
    result = chat_svc._check_result(outcome, [kc])

    assert result.components[0].score is None
    assert result.components[0].failure_kind is None


def test_a_component_the_grader_did_score_reports_its_own_number() -> None:
    kc_a = KC(id=uuid.uuid4(), topic_id=uuid.uuid4(), slug="a", name="Projection")
    kc_b = KC(id=uuid.uuid4(), topic_id=uuid.uuid4(), slug="b", name="Least squares")
    outcome = _outcome(
        Item(id=uuid.uuid4(), item_type=ItemType.SHORT, stem="q"),
        GradeResult(
            score=0.5,
            correct=False,
            detail={},
            component_scores={kc_a.id: 1.0, kc_b.id: 0.0},
        ),
    )
    result = chat_svc._check_result(outcome, [kc_a, kc_b])

    assert {c.kc_name: c.score for c in result.components} == {
        "Projection": pytest.approx(1.0),
        "Least squares": pytest.approx(0.0),
    }


@pytest.mark.parametrize("kind", [FailureKind.NONE, FailureKind.INCOMPLETE])
def test_a_kind_that_names_no_failure_is_reported_as_no_diagnosis(kind: FailureKind) -> None:
    """ "We could not tell" and "it was fine" are both wrong things to render as a diagnosis —
    one because nothing was shown, the other because there is nothing to fix."""
    kc = KC(id=uuid.uuid4(), topic_id=uuid.uuid4(), slug="v", name="Velocity")
    outcome = _outcome(
        Item(id=uuid.uuid4(), item_type=ItemType.SHORT, stem="q"),
        GradeResult(
            score=0.0,
            correct=False,
            detail={},
            diagnoses={kc.id: Diagnosis(kind=kind, confidence=0.7, evidence="something")},
        ),
    )
    assert chat_svc._check_result(outcome, [kc]).components[0].failure_kind is None


def test_a_real_diagnosis_reaches_the_learner_without_its_confidence() -> None:
    """A language model's self-reported confidence is not calibrated, and putting a number on
    it tells the learner it is (S09)."""
    kc = KC(id=uuid.uuid4(), topic_id=uuid.uuid4(), slug="v", name="Velocity")
    outcome = _outcome(
        Item(id=uuid.uuid4(), item_type=ItemType.SHORT, stem="q"),
        GradeResult(
            score=0.2,
            correct=False,
            detail={},
            diagnoses={
                kc.id: Diagnosis(
                    kind=FailureKind.PROCEDURAL,
                    confidence=0.81,
                    evidence="The method was right; a sign was dropped.",
                )
            },
        ),
    )
    component = chat_svc._check_result(outcome, [kc]).components[0]

    assert component.failure_kind == "procedural"
    assert component.failure_detail == "The method was right; a sign was dropped."
    assert "0.81" not in component.model_dump_json()


def _frames(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_the_graded_result_reaches_the_client_on_the_stream(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """End to end: the report is only worth building if it leaves the server."""
    kc = await _kc(db_session)
    item, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), kc
    )
    assert item is not None
    conversation = Conversation(
        learner_id=api_learner.id,
        # A committed goal is what routes the turn to the tutor rather than the refinement
        # gate — the check only exists on that flow.
        goal="Understand velocity",
        phase=ConversationPhase.AWAITING_ANSWER,
        active_item_id=item.id,
    )
    db_session.add(conversation)
    await db_session.commit()

    client, _ = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)
    app.dependency_overrides[get_llm_client] = lambda: client
    try:
        r = await api_client.post(
            f"/api/v1/conversations/{conversation.id}/messages",
            json={"content": "Velocity is speed with a direction."},
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert r.status_code == 200, r.text
    frames = _frames(r.text)
    done = [f for f in frames if f["type"] == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0]["check_result"] is not None
    assert done[0]["check_result"]["item_id"] == str(item.id)
    assert done[0]["check_result"]["correct"] is True


async def test_a_turn_that_grades_nothing_carries_no_result(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Null on every turn that did not grade an answer, which is most of them."""
    conversation = Conversation(learner_id=api_learner.id, goal="Understand velocity")
    db_session.add(conversation)
    await db_session.commit()

    client, _ = _role_client(fast="", smart="")
    app.dependency_overrides[get_llm_client] = lambda: client
    try:
        r = await api_client.post(
            f"/api/v1/conversations/{conversation.id}/messages", json={"content": "hello"}
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    frames = _frames(r.text)
    done = [f for f in frames if f["type"] == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0]["check_result"] is None
