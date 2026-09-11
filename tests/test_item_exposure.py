"""Practice gives a different question next time, and says what mastery rests on (S14).

Bank selection was `ORDER BY created_at`, which is stable — so a learner practising a KC twice
got the same question twice. After the first attempt that measures recall of one question, and
because every attempt still updated mastery, re-answering what the learner had just been told
drove the estimate up.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import mastery
from app.learning.mastery import Observation
from app.models.assessment import Item, ItemKC, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.services import analytics as analytics_svc
from app.services import assessment as svc
from tests.querycount import count_queries

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"exp-{uuid.uuid4().hex[:8]}")
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


async def _item(session: AsyncSession, kc: KC, stem: str) -> Item:
    item = Item(item_type=ItemType.MCQ, stem=stem, difficulty=0.0, answer_key={"correct": 0})
    session.add(item)
    await session.flush()
    session.add(ItemKC(item_id=item.id, kc_id=kc.id, weight=1.0))
    await session.flush()
    return item


async def _answer(
    session: AsyncSession,
    learner: Learner,
    kc: KC,
    item: Item,
    *,
    when: datetime,
    hints: int | None = None,
    priors: int = 0,
) -> None:
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            item_id=item.id,
            hints_used=hints,
            prior_attempts=priors,
        ),
        now=when,
    )
    await session.flush()


# --- selection --------------------------------------------------------------


async def test_a_second_visit_gets_a_different_question(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    first = await _item(db_session, kc, "the oldest one")
    second = await _item(db_session, kc, "a newer one")

    chosen = await svc.find_item_for_kc(db_session, kc.id, learner_id=learner.id)
    assert chosen is not None and chosen.id == first.id, "unseen items keep bank order"

    await _answer(db_session, learner, kc, first, when=T0)
    again = await svc.find_item_for_kc(db_session, kc.id, learner_id=learner.id)

    assert again is not None and again.id == second.id


async def test_the_least_recently_answered_comes_back_first(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    a = await _item(db_session, kc, "a")
    b = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, a, when=T0)
    await _answer(db_session, learner, kc, b, when=T0 + timedelta(days=3))

    # Both seen, so freshness is which was longest ago — not which was created first.
    chosen = await svc.find_item_for_kc(db_session, kc.id, learner_id=learner.id)
    assert chosen is not None and chosen.id == a.id


async def test_another_learners_exposure_does_not_move_this_one(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    other = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    first = await _item(db_session, kc, "a")
    await _item(db_session, kc, "b")
    await _answer(db_session, other, kc, first, when=T0)

    chosen = await svc.find_item_for_kc(db_session, kc.id, learner_id=learner.id)
    assert chosen is not None and chosen.id == first.id


async def test_freshness_respects_the_requested_type(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    mcq = await _item(db_session, kc, "mcq")
    short = Item(item_type=ItemType.SHORT, stem="short", difficulty=0.0, answer_key={})
    db_session.add(short)
    await db_session.flush()
    db_session.add(ItemKC(item_id=short.id, kc_id=kc.id, weight=1.0))
    await db_session.flush()
    await _answer(db_session, learner, kc, mcq, when=T0)

    # The unanswered SHORT is fresher, but a caller asking for MCQ must still get one.
    chosen = await svc.find_item_for_kc(
        db_session, kc.id, learner_id=learner.id, item_type=ItemType.MCQ
    )
    assert chosen is not None and chosen.id == mcq.id


async def test_selection_does_not_cost_a_query_per_candidate(db_session: AsyncSession) -> None:
    """Exposure is a correlated subquery, so the cost is the same whatever the bank holds.

    Asserted as "does not grow" rather than a fixed number: the constant part is the eager
    load of the chosen item's KC links, which belongs to the caller's needs, not to this."""
    learner = await _learner(db_session)
    _subject, small = await _kc(db_session)
    _subject_b, large = await _kc(db_session)
    for i in range(2):
        await _item(db_session, small, f"small {i}")
    for i in range(12):
        await _item(db_session, large, f"large {i}")

    with count_queries(db_session) as few:
        await svc.find_item_for_kc(db_session, small.id, learner_id=learner.id)
    with count_queries(db_session) as many:
        await svc.find_item_for_kc(db_session, large.id, learner_id=learner.id)

    assert len(many) == len(few), many


# --- what the estimate rests on ---------------------------------------------


async def test_one_question_answered_twice_shows_neither_transfer_nor_retention(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    only = await _item(db_session, kc, "the only question")
    await _answer(db_session, learner, kc, only, when=T0)
    await _answer(db_session, learner, kc, only, when=T0 + timedelta(minutes=4), priors=1)

    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()

    assert ev.attempts == 2
    assert ev.distinct_items == 1
    assert ev.unassisted_items == 1, "the re-look is not an independent demonstration"
    assert not ev.transfer_shown
    assert not ev.retention_shown(min_days=1.0)


async def test_a_re_look_does_not_extend_the_unaided_span(db_session: AsyncSession) -> None:
    """The span is measured to the last *independent* demonstration.

    Re-answering a question you were just shown is the thing S13 established is not evidence
    of unaided capability; letting it move the clock would let a sitting spent repeating one
    question look like a capability that lasted.
    """
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    only = await _item(db_session, kc, "one question")
    await _answer(db_session, learner, kc, only, when=T0)
    await _answer(db_session, learner, kc, only, when=T0 + timedelta(minutes=12), priors=1)

    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()

    assert ev.span_days == 0.0, "the clock stops at the last unaided attempt, not the last one"


async def test_two_different_questions_unaided_show_transfer(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    a = await _item(db_session, kc, "a")
    b = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, a, when=T0)
    await _answer(db_session, learner, kc, b, when=T0 + timedelta(minutes=5))

    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()

    assert ev.unassisted_items == 2
    assert ev.transfer_shown
    assert not ev.retention_shown(min_days=1.0), "same sitting is not retention"


async def test_a_hinted_answer_is_not_an_unassisted_item(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    a = await _item(db_session, kc, "a")
    b = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, a, when=T0)
    await _answer(db_session, learner, kc, b, when=T0 + timedelta(minutes=5), hints=2)

    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()

    assert ev.distinct_items == 2
    assert ev.unassisted_items == 1
    assert not ev.transfer_shown, "being walked through a second question is not transfer"


async def test_an_unaided_answer_days_later_shows_retention(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    a = await _item(db_session, kc, "a")
    b = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, a, when=T0)
    await _answer(db_session, learner, kc, b, when=T0 + timedelta(days=9))

    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()

    assert ev.span_days is not None and ev.span_days > 8
    assert ev.retention_shown(min_days=1.0)
    assert not ev.retention_shown(min_days=30.0)


async def test_a_delay_spent_being_hinted_is_not_retention(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    a = await _item(db_session, kc, "a")
    b = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, a, when=T0, hints=1)
    await _answer(db_session, learner, kc, b, when=T0 + timedelta(days=9), hints=3)

    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()

    assert ev.span_days is None
    assert not ev.retention_shown(min_days=1.0)


async def test_a_kc_with_no_attempts_has_no_row(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    assert await mastery.kc_evidence(db_session, learner.id, [kc.id]) == {}


async def test_evidence_is_per_kc(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    _subject, kc_a = await _kc(db_session)
    _subject_b, kc_b = await _kc(db_session)
    item_a = await _item(db_session, kc_a, "a")
    item_b = await _item(db_session, kc_b, "b")
    await _answer(db_session, learner, kc_a, item_a, when=T0)
    await _answer(db_session, learner, kc_b, item_b, when=T0)

    evidence = await mastery.kc_evidence(db_session, learner.id, [kc_a.id, kc_b.id])

    assert set(evidence) == {kc_a.id, kc_b.id}
    assert all(e.distinct_items == 1 for e in evidence.values())


# --- and it reaches the page that shows the estimate ------------------------


async def test_the_mastery_page_reports_what_the_estimate_rests_on(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    subject, kc = await _kc(db_session)
    a = await _item(db_session, kc, "a")
    b = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, a, when=T0)
    await _answer(db_session, learner, kc, b, when=T0 + timedelta(days=5))

    read = await analytics_svc.subject_mastery(db_session, learner.id, subject.id)
    (kc_read,) = read.topics[0].kcs

    assert kc_read.distinct_items == 2
    assert kc_read.unassisted_items == 2
    assert kc_read.transfer_shown
    assert kc_read.retention_shown


async def test_the_mastery_page_stays_flat_in_queries(db_session: AsyncSession) -> None:
    """S62 got this page to a fixed cost; reading evidence must not reintroduce per-KC work."""
    learner = await _learner(db_session)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add(subject)
    await db_session.flush()
    for t in range(3):
        topic = Topic(subject_id=subject.id, slug=f"t{t}-{uuid.uuid4().hex[:6]}", name=f"T{t}")
        db_session.add(topic)
        await db_session.flush()
        for k in range(4):
            db_session.add(
                KC(topic_id=topic.id, slug=f"k{t}{k}-{uuid.uuid4().hex[:6]}", name=f"K{t}{k}")
            )
    await db_session.flush()

    with count_queries(db_session) as counter:
        await analytics_svc.subject_mastery(db_session, learner.id, subject.id)

    assert len(counter) == 4, counter


def test_the_retention_floor_is_configured_not_hardcoded() -> None:
    assert get_settings().retention_min_days > 0
