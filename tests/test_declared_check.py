"""The tutor can say "this question counts" (S15).

A conversational check was posed only from the lesson plan's active step. That covers the case
the plan anticipated and misses the one that makes conversation worth having: the tutor notices
something, asks about it, the learner answers — and none of it reached the tracer, because
nothing in the exchange was ever an item.

The obstacle was attribution, not detection. A question in prose carries no component and no
stated standard, so an answer to it cannot be scored against anything or credited to anything,
and guessing either from the text would be inventing evidence.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import declared_check
from app.models.assessment import ItemOrigin, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.services import chat as svc

# --- the marker itself ----------------------------------------------------------------------


def test_a_declaration_is_lifted_out_of_the_reply() -> None:
    reply = "Momentum is mass times velocity.\n[[CHECK: Momentum :: Why is momentum a vector?]]"
    cleaned, declared = declared_check.extract(reply)

    assert declared is not None
    assert declared.component == "Momentum"
    assert declared.question == "Why is momentum a vector?"
    assert "[[CHECK" not in cleaned
    assert cleaned == "Momentum is mass times velocity."


def test_an_ordinary_reply_is_untouched() -> None:
    reply = "Momentum is mass times velocity. Does that make sense so far?"
    cleaned, declared = declared_check.extract(reply)

    assert declared is None
    assert cleaned == reply, "a reply with no marker must come through byte-for-byte"


def test_the_marker_never_reaches_the_learner() -> None:
    """It is addressed to the system, and somebody reading their own transcript should not
    find machinery in it."""
    cleaned, _ = declared_check.extract("Here you go. [[CHECK: X :: What is X?]] Good luck.")
    assert "CHECK" not in cleaned


def test_a_second_declaration_is_stripped_rather_than_left_on_screen() -> None:
    """Two declarations is a disobeyed instruction either way. Taking the first and removing
    the rest leaves one question on screen and one graded, rather than two of neither."""
    reply = "[[CHECK: A :: First question?]] and [[CHECK: B :: Second question?]]"
    cleaned, declared = declared_check.extract(reply)

    assert declared is not None and declared.component == "A"
    assert "CHECK" not in cleaned


def test_a_half_written_declaration_is_not_a_check() -> None:
    """The reply streams token by token; an unterminated marker must not become an item."""
    _, declared = declared_check.extract("Nearly there [[CHECK: Momentum :: Why is")
    assert declared is None


def test_an_empty_question_is_not_a_check() -> None:
    _, declared = declared_check.extract("[[CHECK: Momentum ::   ]]")
    assert declared is None


def test_the_marker_tolerates_the_spacing_a_model_actually_produces() -> None:
    _, declared = declared_check.extract("[[check:  Momentum  ::  Why is it a vector?  ]]")
    assert declared is not None
    assert declared.component == "Momentum"
    assert declared.question == "Why is it a vector?"


# --- turning one into a gradable item ---------------------------------------------------------


async def _subject_with_kc(session: AsyncSession) -> tuple[Learner, Subject, KC]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="momentum", name="Momentum")
    session.add(kc)
    await session.flush()
    return learner, subject, kc


async def test_a_declared_question_becomes_the_item_verbatim(db_session: AsyncSession) -> None:
    """The learner is shown the question from the marker, so the thing asked and the thing
    graded cannot drift apart — there is only one of them."""
    learner, subject, kc = await _subject_with_kc(db_session)

    item = await svc._materialise_declared_check(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        declared=declared_check.DeclaredCheck(
            component="momentum", question="Why is momentum a vector?"
        ),
    )

    assert item is not None
    assert item.stem == "Why is momentum a vector?"
    assert item.item_type == ItemType.SHORT
    assert [link.kc_id for link in item.kc_links] == [kc.id]


async def test_a_declared_item_stays_out_of_the_shared_bank(db_session: AsyncSession) -> None:
    """A question improvised for one conversation has no reviewed rubric, no difficulty target
    and no provenance beyond one exchange — other learners should not be assessed against it."""
    learner, subject, _kc = await _subject_with_kc(db_session)

    item = await svc._materialise_declared_check(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        declared=declared_check.DeclaredCheck(component="Momentum", question="Why?"),
    )

    assert item is not None
    assert item.origin == ItemOrigin.LEARNER
    assert item.author_learner_id == learner.id


async def test_a_component_that_resolves_to_nothing_is_dropped(db_session: AsyncSession) -> None:
    """The tutor sees the conversation, not the graph, so it can name something true and
    irrelevant — or something that is not a component at all."""
    learner, subject, _kc = await _subject_with_kc(db_session)

    item = await svc._materialise_declared_check(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        declared=declared_check.DeclaredCheck(
            component="Thermodynamics", question="What is entropy?"
        ),
    )

    assert item is None


async def test_a_component_from_another_subject_is_not_borrowed(
    db_session: AsyncSession,
) -> None:
    """Resolution is scoped to this conversation's subject: an answer credited to a component
    the conversation had nothing to do with is evidence about the wrong skill."""
    learner, subject, _kc = await _subject_with_kc(db_session)
    _other_learner, other_subject, other_kc = await _subject_with_kc(db_session)

    item = await svc._materialise_declared_check(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        declared=declared_check.DeclaredCheck(component=other_kc.name, question="Why?"),
    )

    # Both subjects happen to name their component "Momentum", so this resolves — to *this*
    # subject's, which is the point.
    assert item is not None
    assert [link.kc_id for link in item.kc_links] != [other_kc.id]
    assert other_subject.id != subject.id
