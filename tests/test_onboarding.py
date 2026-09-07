"""Onboarding orchestration service: goal-refinement gate + curriculum generation (ephemeral, no Conversation)."""

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.curriculum import CurriculumProposal
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.models.chat import LLMCall
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.services.knowledge import create_subject_with_graph, list_kcs_for_subject, list_topics
from app.services.onboarding import generate_curriculum_for_onboarding, run_goal_refinement_turn
from app.services.turn_common import TurnEvent

REPLY = "Learn the fundamentals of linear algebra including vectors and matrices."
CURRICULUM_REPLY = json.dumps(
    {
        "subject_name": "Linear Algebra",
        "subject_description": "Foundation of mathematical structures and transformations",
        "topics": [
            {
                "name": "Vectors",
                "description": "Vector spaces and operations",
                "kcs": [
                    {"name": "Vector Addition", "description": "Adding vectors in Rn"},
                    {"name": "Dot Product", "description": "Scalar product of vectors"},
                ],
            },
            {
                "name": "Matrices",
                "description": "Matrix algebra and transformations",
                "kcs": [
                    {"name": "Matrix Multiplication", "description": "Multiplying matrices"},
                    {"name": "Determinants", "description": "Computing determinants"},
                ],
            },
        ],
    }
)


async def _drain(
    llm_client,
    session_id: str,
    user_content: str,
    *,
    satisfied: bool = False,
    resume: bool = False,
    learner_id: uuid.UUID | None = None,
) -> list[TurnEvent]:
    """Drain all events from a goal-refinement turn."""
    return [
        ev
        async for ev in run_goal_refinement_turn(
            llm=llm_client,
            session_id=session_id,
            user_content=user_content,
            satisfied=satisfied,
            resume=resume,
            learner_id=learner_id,
        )
    ]


class TestRunGoalRefinementTurn:
    """Tests for run_goal_refinement_turn orchestration."""

    async def test_the_gate_records_what_the_negotiation_cost(
        self, db_session: AsyncSession
    ) -> None:
        """Onboarding discards its transcript by design; that is not a reason to discard cost."""
        learner = Learner(handle=f"gate-{uuid.uuid4().hex[:8]}")
        db_session.add(learner)
        await db_session.flush()

        await _drain(
            fake_llm_client(REPLY),
            str(uuid.uuid4()),
            user_content="I want to learn linear algebra",
            learner_id=learner.id,
        )

        calls = (
            await db_session.scalars(select(LLMCall).where(LLMCall.learner_id == learner.id))
        ).all()
        assert [c.role for c in calls] == ["fast"]
        assert calls[0].input_tokens > 0

    async def test_start_streams_tokens_and_awaits_reply(self) -> None:
        """First turn streams token events and ends with awaiting_reply."""
        llm = fake_llm_client(REPLY)
        session_id = str(uuid.uuid4())

        events = await _drain(llm, session_id, user_content="I want to learn linear algebra")

        # Should have token events
        tokens = [e for e in events if e.type == "token"]
        assert len(tokens) > 0
        assert "".join(e.text for e in tokens) == REPLY

        # Should have awaiting_reply event
        awaiting = next((e for e in events if e.type == "awaiting_reply"), None)
        assert awaiting is not None
        assert awaiting.text == REPLY
        assert awaiting.detail == "round 1"

    async def test_resume_continues_negotiation(self) -> None:
        """Resuming from same session_id continues with re-proposing on unsatisfied resume."""
        llm = fake_llm_client(REPLY)
        session_id = str(uuid.uuid4())

        # Start
        events = await _drain(llm, session_id, user_content="I want to learn algebra")
        awaiting = next((e for e in events if e.type == "awaiting_reply"), None)
        assert awaiting is not None

        # Resume with feedback (unsatisfied -> should re-propose)
        events2 = await _drain(
            llm,
            session_id,
            user_content="Yes, but more on linear equations",
            resume=True,
        )
        # Should have awaiting_reply event (re-proposed) on unsatisfied resume
        awaiting2 = next((e for e in events2 if e.type == "awaiting_reply"), None)
        assert awaiting2 is not None
        assert awaiting2.detail == "round 2"

    async def test_satisfied_commits_goal(self) -> None:
        """Setting satisfied=True commits the goal without streaming new tokens."""
        llm = fake_llm_client(REPLY)
        session_id = str(uuid.uuid4())

        # Start
        await _drain(llm, session_id, user_content="Learn algebra")

        # Resume with satisfaction
        events2 = await _drain(
            llm,
            session_id,
            user_content="Yes, that works",
            satisfied=True,
            resume=True,
        )

        # Should have committed event
        committed = next((e for e in events2 if e.type == "committed"), None)
        assert committed is not None
        assert committed.text == REPLY
        assert committed.detail == "accepted"

        # No tokens should be streamed on commit
        tokens = [e for e in events2 if e.type == "token"]
        assert len(tokens) == 0


class TestGenerateCurriculumForOnboarding:
    """Tests for generate_curriculum_for_onboarding."""

    async def test_generate_without_sources(self, db_session: AsyncSession) -> None:
        """Curriculum generation without source_ids (materials=None)."""
        llm = fake_llm_client(CURRICULUM_REPLY)

        # Create a learner
        learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
        db_session.add(learner)
        await db_session.flush()

        result = await generate_curriculum_for_onboarding(
            session=db_session,
            llm=llm,
            goal="Learn linear algebra",
            source_ids=None,
            learner_id=learner.id,
        )

        assert result is not None
        assert isinstance(result, CurriculumProposal)
        assert result.subject_name == "Linear Algebra"
        assert len(result.topics) == 2
        assert result.topics[0].name == "Vectors"

    async def test_generate_with_empty_sources_list(self, db_session: AsyncSession) -> None:
        """Curriculum generation with empty source_ids list (materials=None)."""
        llm = fake_llm_client(CURRICULUM_REPLY)

        learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
        db_session.add(learner)
        await db_session.flush()

        result = await generate_curriculum_for_onboarding(
            session=db_session,
            llm=llm,
            goal="Learn linear algebra",
            source_ids=[],
            learner_id=learner.id,
        )

        assert result is not None
        assert isinstance(result, CurriculumProposal)
        assert result.subject_name == "Linear Algebra"

    async def test_generate_with_real_sources(self, db_session: AsyncSession) -> None:
        """Curriculum generation with real source_ids (tests excerpt-fetch path).

        This test exercises the branch where generate_curriculum_for_onboarding
        calls retrieval.retrieve and builds materials from hit.text.
        """
        # Set up learner
        learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
        db_session.add(learner)
        await db_session.flush()

        # Create a source
        source = Source(
            learner_id=learner.id,
            kind=SourceKind.FILE,
            origin="linear_algebra.txt",
            content_type="text/plain",
            status=SourceStatus.DONE,
            subject_id=None,
            meta={},
        )
        db_session.add(source)
        await db_session.flush()

        # Create an LLM client for both curriculum generation and embedding
        llm = fake_llm_client(CURRICULUM_REPLY)

        # Create a chunk with embedding (following test_retrieval.py pattern)
        # Use text with keywords that match the goal for keyword-path retrieval
        chunk_text = (
            "linear algebra fundamentals: vectors and vector spaces, matrices and transformations"
        )
        embedding = (await llm.embed(ModelRole.EMBED, [chunk_text])).vectors[0]
        chunk = Chunk(
            source_id=source.id,
            ordinal=0,
            text=chunk_text,
            embedding=embedding,
            provenance={"source_id": str(source.id), "method": "text"},
        )
        db_session.add(chunk)
        await db_session.flush()

        # Call generate_curriculum_for_onboarding with real source_ids
        # The query will be the goal; retrieval will use keyword matching to find the chunk
        result = await generate_curriculum_for_onboarding(
            session=db_session,
            llm=llm,
            goal="Learn linear algebra",
            source_ids=[source.id],
            learner_id=learner.id,
        )

        # Assert a non-None CurriculumProposal comes back (proving the excerpt path runs end to end)
        assert result is not None
        assert isinstance(result, CurriculumProposal)
        # The curriculum should reflect the injected reply
        assert result.subject_name == "Linear Algebra"
        assert len(result.topics) == 2
        assert result.topics[0].name == "Vectors"


async def test_onboarding_end_to_end(db_session: AsyncSession) -> None:
    """End-to-end onboarding: refine goal -> generate curriculum -> commit graph -> verify.

    Ties Tasks 1-4 together: drives goal refinement to a committed goal, generates
    a curriculum, commits the Subject/Topic/KC graph, and asserts it persisted.
    """
    # 1. Create a Learner
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    # 2. Refine goal: start then satisfy
    refine_llm = fake_llm_client(REPLY)
    session_id = str(uuid.uuid4())

    # Start refinement
    events = await _drain(refine_llm, session_id, user_content="I want to learn linear algebra")
    awaiting = next((e for e in events if e.type == "awaiting_reply"), None)
    assert awaiting is not None, "Should have awaiting_reply event from first refinement turn"
    assert awaiting.text == REPLY

    # Resume with satisfaction to commit the goal
    events2 = await _drain(
        refine_llm,
        session_id,
        user_content="Yes, that's what I need",
        satisfied=True,
        resume=True,
    )
    committed = next((e for e in events2 if e.type == "committed"), None)
    assert committed is not None, "Should have committed event after satisfaction"
    assert committed.text == REPLY
    goal = committed.text

    # 3. Generate curriculum from committed goal
    curr_llm = fake_llm_client(CURRICULUM_REPLY)
    proposal = await generate_curriculum_for_onboarding(
        session=db_session,
        llm=curr_llm,
        goal=goal,
        source_ids=None,
        learner_id=learner.id,
    )

    assert proposal is not None, "Curriculum should be generated"
    assert proposal.subject_name == "Linear Algebra"
    assert len(proposal.topics) == 2, "Should have 2 topics"
    assert proposal.topics[0].name == "Vectors"
    assert proposal.topics[1].name == "Matrices"
    # Verify KC structure
    assert len(proposal.topics[0].kcs) == 2, "First topic should have 2 KCs"
    assert len(proposal.topics[1].kcs) == 2, "Second topic should have 2 KCs"

    # 4. Commit the graph: transform proposal into topics_data shape
    topics_data = [
        {
            "name": t.name,
            "description": t.description,
            "kcs": [{"name": kc.name, "description": kc.description} for kc in t.kcs],
        }
        for t in proposal.topics
    ]

    subject = await create_subject_with_graph(
        db_session,
        subject_name=proposal.subject_name,
        subject_description=proposal.subject_description,
        topics_data=topics_data,
        source_ids=None,
        learner_id=learner.id,
    )

    assert subject.id is not None, "Subject should have an id"
    assert subject.name == "Linear Algebra"
    assert subject.slug == "linear-algebra"

    # 5. Verify the graph persisted
    topics = await list_topics(db_session, subject.id)
    assert len(topics) == 2, "Should have 2 topics in DB"
    topic_names = {t.name for t in topics}
    assert topic_names == {"Vectors", "Matrices"}

    kcs = await list_kcs_for_subject(db_session, subject.id)
    assert len(kcs) == 4, "Should have 4 KCs total across all topics"
    kc_names = {kc.name for kc in kcs}
    assert kc_names == {"Vector Addition", "Dot Product", "Matrix Multiplication", "Determinants"}
