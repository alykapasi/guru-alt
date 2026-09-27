"""Query counts that must not grow with a learner's history or graph (S62).

Analytics loaded per-KC estimates and then the rollups reloaded every one of them; the notes
index queried per topic. Costs like these are invisible against three-row fixtures — the
answer is right either way — and only a real account notices. A query count is the part that
can be measured deterministically: no clock, no cache, no machine to compare against.

Each budget below is asserted at two graph sizes. The number itself is a ceiling with slack;
what the test is really asserting is the *shape* — that doubling the graph does not double the
queries.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.services import analytics as analytics_svc
from app.services import notes as notes_svc
from tests.querycount import count_queries


async def _graph(
    session: AsyncSession, *, topics: int, kcs_per_topic: int
) -> tuple[Learner, uuid.UUID]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    slug = f"s-{uuid.uuid4().hex[:8]}"
    subject = Subject(name=slug, slug=slug)
    session.add(subject)
    await session.flush()
    for t in range(topics):
        topic = Topic(subject_id=subject.id, name=f"T{t}", slug=f"t{t}-{uuid.uuid4().hex[:6]}")
        session.add(topic)
        await session.flush()
        for k in range(kcs_per_topic):
            session.add(
                KC(
                    topic_id=topic.id,
                    name=f"K{t}.{k}",
                    description="d",
                    slug=f"k{t}{k}-{uuid.uuid4().hex[:6]}",
                )
            )
    await session.commit()
    return learner, subject.id


async def test_subject_mastery_does_not_query_per_component(db_session: AsyncSession) -> None:
    """It used to run one query per topic for its KCs, one per KC for the estimate, and then
    the topic rollup re-ran both — so a 4x4 subject cost about forty round trips."""
    learner, small = await _graph(db_session, topics=2, kcs_per_topic=2)
    with count_queries(db_session) as small_count:
        await analytics_svc.subject_mastery(db_session, learner.id, small)

    _, large = await _graph(db_session, topics=8, kcs_per_topic=8)
    with count_queries(db_session) as large_count:
        await analytics_svc.subject_mastery(db_session, learner.id, large)

    assert len(large_count) == len(small_count), (
        f"query count grew with the graph\nsmall: {small_count}\nlarge: {large_count}"
    )
    assert len(small_count) <= 4, small_count
    # For the record: this was 15 for the 2x2 and 147 for the 8x8 before the fix.


async def test_the_notes_index_does_not_query_per_topic(db_session: AsyncSession) -> None:
    """It used to run about six queries per topic — the note, two learner-global profile
    dimensions re-read every time round the loop, two existence probes (one of them asking a
    subject-wide question once per topic), and a render lookup."""
    learner, small = await _graph(db_session, topics=2, kcs_per_topic=2)
    with count_queries(db_session) as small_count:
        await notes_svc.notes_index(db_session, learner.id, small)

    _, large = await _graph(db_session, topics=8, kcs_per_topic=8)
    with count_queries(db_session) as large_count:
        await notes_svc.notes_index(db_session, learner.id, large)

    assert len(large_count) == len(small_count), (
        f"query count grew with the graph\nsmall: {small_count}\nlarge: {large_count}"
    )
    # One constant lookup authorizes the subject before any private topic names are returned,
    # and one reads the learner's note-format setting for the subject (S02). The budget still
    # proves graph size cannot add queries.
    assert len(small_count) <= 9, small_count


async def test_a_subject_rollup_does_not_query_per_topic(db_session: AsyncSession) -> None:
    """rollup_subject re-read each topic's KCs and each KC's state, having already read both
    to build the topic estimate it was aggregating."""
    from app.learning import mastery

    learner, small = await _graph(db_session, topics=2, kcs_per_topic=2)
    with count_queries(db_session) as small_count:
        await mastery.rollup_subject(db_session, learner.id, small)

    _, large = await _graph(db_session, topics=8, kcs_per_topic=8)
    with count_queries(db_session) as large_count:
        await mastery.rollup_subject(db_session, learner.id, large)

    assert len(large_count) == len(small_count), (
        f"query count grew with the graph\nsmall: {small_count}\nlarge: {large_count}"
    )
    assert len(small_count) <= 3, small_count


async def test_profile_refresh_does_not_query_per_piece_of_evidence(
    db_session: AsyncSession,
) -> None:
    """S43: the window bounds what is read; this bounds how many round trips reading it takes."""
    from app.llm.registry import fake_llm_client
    from app.models.chat import Conversation, Message
    from app.models.learner import Learner
    from app.models.learning import LearningEvent
    from app.services import profile as profile_svc

    async def learner_with(n: int) -> uuid.UUID:
        learner = Learner(handle=f"q-{uuid.uuid4().hex[:8]}")
        db_session.add(learner)
        await db_session.flush()
        conv = Conversation(learner_id=learner.id)
        db_session.add(conv)
        await db_session.flush()
        for i in range(n):
            db_session.add(Message(conversation_id=conv.id, role="user", content=f"m{i}"))
            db_session.add(
                LearningEvent(
                    learner_id=learner.id,
                    event_type="observation",
                    payload={"score": 1.0, "difficulty": 0.5, "latency_ms": 4000, "hints_used": 0},
                )
            )
        await db_session.commit()
        return learner.id

    llm = fake_llm_client("{}")
    # Both big enough for every dimension that can be computed from this history: each
    # dimension is one read and one write, bounded by the catalog; this is about evidence.
    small, large = await learner_with(40), await learner_with(120)
    with count_queries(db_session) as small_count:
        await profile_svc.refresh_profile(db_session, small, llm)
    with count_queries(db_session) as large_count:
        await profile_svc.refresh_profile(db_session, large, llm)

    assert len(large_count) == len(small_count), (
        f"query count grew with the history\nsmall: {small_count}\nlarge: {large_count}"
    )
