"""Why an answer failed, in terms something downstream can branch on (S09).

Grading returned a score and a sentence of prose. "0.4, the learner confused the two forms"
and "0.4, the learner never learned what a basis is" are the same number and the same shape,
so nothing could choose different help for them — and one of those two needs the plan changed
rather than the explanation reworded.
"""

import json
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import diagnosis as diag
from app.learning.diagnosis import FailureKind
from app.learning.rubric_grading import GradedComponent, grade_open
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef, Usage
from app.models.assessment import Item, ItemKC, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as svc

ANSWER = "I multiplied the matrix by its transpose and then took the inverse."


# --- the pure layer ----------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["notation", "procedural", "conceptual", "prerequisite", "incomplete"],
)
def test_every_kind_in_the_vocabulary_round_trips(kind: str) -> None:
    parsed = diag.parse({"kind": kind, "confidence": 0.5}, response_text=ANSWER)
    assert parsed.kind == FailureKind(kind)


def test_an_unknown_kind_degrades_rather_than_raising() -> None:
    # A grade is still a grade without a diagnosis; losing the score because the extra field
    # came back malformed trades something depended on for something merely useful.
    assert diag.parse({"kind": "vibes"}, response_text=ANSWER).kind is FailureKind.NONE
    assert diag.parse("not an object", response_text=ANSWER).kind is FailureKind.NONE
    assert diag.parse(None, response_text=ANSWER).kind is FailureKind.NONE


def test_a_correct_component_carries_nothing_else() -> None:
    parsed = diag.parse(
        {"kind": "none", "confidence": 0.9, "evidence": "whatever"}, response_text=ANSWER
    )
    assert parsed.kind is FailureKind.NONE
    assert parsed.evidence == ""
    assert not parsed.actionable


def test_only_the_kinds_that_name_a_failure_are_actionable() -> None:
    """`incomplete` is the trap: it looks like a failure and says the least of any label —
    nothing was demonstrated either way, so it must not be read as evidence they cannot do it."""
    assert diag.parse({"kind": "conceptual"}, response_text=ANSWER).actionable
    assert not diag.parse({"kind": "incomplete"}, response_text=ANSWER).actionable


def test_a_quote_the_learner_actually_wrote_is_marked_verbatim() -> None:
    parsed = diag.parse(
        {"kind": "procedural", "evidence": "took the inverse"}, response_text=ANSWER
    )
    assert parsed.evidence_verbatim


def test_a_reflowed_quote_still_counts() -> None:
    # Reflowing a quote is not the thing worth catching; inventing one is.
    parsed = diag.parse(
        {"kind": "procedural", "evidence": "Took   The\nInverse"}, response_text=ANSWER
    )
    assert parsed.evidence_verbatim


def test_an_invented_quote_is_kept_but_flagged() -> None:
    """A model asked to justify a judgement produces a quote whether or not one exists. Not a
    reason to discard the diagnosis — a reason not to show it to the learner as their words."""
    parsed = diag.parse(
        {"kind": "conceptual", "evidence": "eigenvalues are always positive"},
        response_text=ANSWER,
    )
    assert parsed.evidence == "eigenvalues are always positive"
    assert not parsed.evidence_verbatim


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0.5, 0.5), ("0.25", 0.25), (5.0, 1.0), (-2.0, 0.0), ("high", 0.0), (None, 0.0)],
)
def test_confidence_is_clamped_and_never_raises(raw: object, expected: float) -> None:
    parsed = diag.parse({"kind": "conceptual", "confidence": raw}, response_text=ANSWER)
    assert parsed.confidence == pytest.approx(expected)


def test_a_named_prerequisite_survives_as_written() -> None:
    # Free text on purpose: the grader sees the question, not the graph.
    parsed = diag.parse(
        {"kind": "prerequisite", "prerequisite": "Matrix inverses"}, response_text=ANSWER
    )
    assert parsed.prerequisite == "Matrix inverses"


# --- the grader --------------------------------------------------------------


class _Recording(FakeProvider):
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
        return ChatResponse(
            content=self._reply, usage=Usage(input_tokens=1, output_tokens=1), model=model
        )


def _client(reply: str) -> tuple[LLMClient, list[str | None]]:
    systems: list[str | None] = []
    provider = _Recording(reply, systems)
    return LLMClient(
        {"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}
    ), systems


def _comps(n: int) -> list[GradedComponent]:
    return [GradedComponent(kc_id=uuid.uuid4(), name=f"KC{i}") for i in range(1, n + 1)]


async def test_a_single_component_grade_still_says_why() -> None:
    """The top-level shape. A one-component question is exactly where a bare number is least
    useful — there is no other component to compare it against."""
    comps = _comps(1)
    reply = json.dumps(
        {
            "score": 0.3,
            "diagnosis": {"kind": "notation", "confidence": 0.8, "evidence": "the inverse"},
        }
    )
    client, systems = _client(reply)
    result, _ = await grade_open(
        client, stem="q", response={"text": ANSWER}, rubric=None, components=comps
    )
    assert result.diagnoses[comps[0].kc_id].kind is FailureKind.NOTATION
    assert systems[0] is not None and "notation" in systems[0]


async def test_each_component_gets_its_own_reason() -> None:
    comps = _comps(2)
    reply = json.dumps(
        {
            "components": [
                {"n": 1, "score": 1.0, "diagnosis": {"kind": "none"}},
                {
                    "n": 2,
                    "score": 0.0,
                    "diagnosis": {"kind": "prerequisite", "prerequisite": "Matrix inverses"},
                },
            ],
            "score": 0.5,
        }
    )
    client, _ = _client(reply)
    result, _ = await grade_open(
        client, stem="q", response={"text": ANSWER}, rubric=None, components=comps
    )
    assert result.diagnoses[comps[0].kc_id].kind is FailureKind.NONE
    assert result.diagnoses[comps[1].kc_id].kind is FailureKind.PREREQUISITE
    assert result.diagnoses[comps[1].kc_id].prerequisite == "Matrix inverses"


async def test_a_broken_diagnosis_does_not_cost_the_mark() -> None:
    comps = _comps(2)
    reply = json.dumps(
        {
            "components": [
                {"n": 1, "score": 0.9, "diagnosis": "nonsense"},
                {"n": 2, "score": 0.2},
            ],
            "score": 0.55,
        }
    )
    client, _ = _client(reply)
    result, _ = await grade_open(
        client, stem="q", response={"text": ANSWER}, rubric=None, components=comps
    )
    assert result.component_scores[comps[0].kc_id] == pytest.approx(0.9)
    assert result.component_scores[comps[1].kc_id] == pytest.approx(0.2)
    assert result.diagnoses[comps[0].kc_id].kind is FailureKind.NONE


async def test_the_grader_checks_the_quote_against_the_real_response() -> None:
    comps = _comps(1)
    reply = json.dumps(
        {"score": 0.2, "diagnosis": {"kind": "conceptual", "evidence": "took the inverse"}}
    )
    client, _ = _client(reply)
    result, _ = await grade_open(
        client, stem="q", response={"text": ANSWER}, rubric=None, components=comps
    )
    assert result.diagnoses[comps[0].kc_id].evidence_verbatim


# --- it reaches the log and survives a replay --------------------------------


async def _setup(session: AsyncSession) -> tuple[Learner, list[KC]]:
    learner = Learner(handle=f"d-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kcs = [KC(topic_id=topic.id, slug=f"k{i}-{uuid.uuid4().hex[:4]}", name=f"K{i}") for i in (1, 2)]
    session.add_all(kcs)
    await session.flush()
    return learner, kcs


async def _answer(session: AsyncSession, learner: Learner, kcs: list[KC], reply: str, attempt=None):
    row = Item(item_type=ItemType.SHORT, stem="Explain the normal equations.")
    session.add(row)
    await session.flush()
    session.add_all([ItemKC(item_id=row.id, kc_id=kc.id) for kc in kcs])
    await session.flush()
    item = await svc.get_item(session, row.id)
    assert item is not None
    client, _ = _client(reply)
    return await svc.answer_item(
        session,
        learner.id,
        item,
        AnswerSubmit(response={"text": ANSWER}, attempt_id=attempt),
        llm=client,
    )


REPLY = json.dumps(
    {
        "components": [
            {"n": 1, "score": 1.0, "diagnosis": {"kind": "none"}},
            {
                "n": 2,
                "score": 0.0,
                "diagnosis": {
                    "kind": "conceptual",
                    "confidence": 0.7,
                    "evidence": "took the inverse",
                },
            },
        ],
        "score": 0.5,
    }
)


async def test_the_reason_is_stored_next_to_the_number(db_session: AsyncSession) -> None:
    """A rationale that only ever reached the response body is a sentence nobody can query."""
    learner, kcs = await _setup(db_session)
    await _answer(db_session, learner, kcs, REPLY)

    events = (await db_session.scalars(select(LearningEvent))).all()
    by_kc = {e.kc_id: e.payload for e in events}
    assert by_kc[kcs[1].id]["diagnosis"]["kind"] == "conceptual"
    assert by_kc[kcs[1].id]["diagnosis"]["evidence_verbatim"] is True
    assert by_kc[kcs[0].id]["diagnosis"]["kind"] == "none"


async def test_a_retry_returns_the_diagnosis_the_first_attempt_got(
    db_session: AsyncSession,
) -> None:
    learner, kcs = await _setup(db_session)
    attempt = uuid.uuid4()
    first, _ = await _answer(db_session, learner, kcs, REPLY, attempt=attempt)

    row = await db_session.scalar(select(Item).where(Item.stem.like("Explain%")))
    assert row is not None
    item = await svc.get_item(db_session, row.id)
    assert item is not None
    client, _ = _client(json.dumps({"score": 0.0}))
    replayed, _ = await svc.answer_item(
        db_session,
        learner.id,
        item,
        AnswerSubmit(response={"text": ANSWER}, attempt_id=attempt),
        llm=client,
    )
    assert replayed.diagnoses == first.diagnoses
    assert replayed.diagnoses[kcs[1].id].kind is FailureKind.CONCEPTUAL


async def test_an_objective_item_offers_no_diagnosis(db_session: AsyncSession) -> None:
    """An MCQ knows the answer was wrong and nothing about why. Manufacturing a reason from
    that would be the failure this item exists to fix, committed on purpose."""
    learner, kcs = await _setup(db_session)
    row = Item(item_type=ItemType.MCQ, stem="q", answer_key={"correct": 0})
    db_session.add(row)
    await db_session.flush()
    db_session.add(ItemKC(item_id=row.id, kc_id=kcs[0].id))
    await db_session.flush()
    item = await svc.get_item(db_session, row.id)
    assert item is not None
    client, _ = _client("")
    result, _ = await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice": 1}), llm=client
    )
    assert result.diagnoses == {}
    assert result.score == 0.0
