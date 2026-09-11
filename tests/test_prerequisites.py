"""Generated curricula carry prerequisite edges, and bad ones degrade instead of failing (S22).

Before this, `create_subject_with_graph` produced topics and KCs and no edges at all, so every
generated subject gave the planner nothing to order by — while the planner's whole job is
ordering by prerequisites.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import prerequisites
from app.learning.curriculum import generate_curriculum
from app.learning.lesson_plan import Edge, topo_sort
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from app.models.knowledge import KC, KCEdge
from app.services import knowledge as svc

# --- the pure layer ---------------------------------------------------------


def test_names_match_across_case_and_spacing() -> None:
    # The model writes prerequisites in prose it generated separately from the name itself,
    # so exact matching drops real edges over a capital letter.
    index, _ = prerequisites.index_by_name([("a", "Vector Spaces"), ("b", "Linear maps")])
    resolved, unresolved = prerequisites.resolve("b", ["  vector   spaces "], index)
    assert resolved == ["a"]
    assert unresolved == []


def test_a_name_outside_the_curriculum_is_dropped_not_raised() -> None:
    index, _ = prerequisites.index_by_name([("a", "Vectors")])
    resolved, unresolved = prerequisites.resolve("a", ["Topology"], index)
    assert resolved == []
    assert unresolved == ["Topology"]


def test_a_kc_requiring_itself_is_dropped() -> None:
    index, _ = prerequisites.index_by_name([("a", "Vectors")])
    resolved, _ = prerequisites.resolve("a", ["Vectors"], index)
    assert resolved == []


def test_a_repeated_name_is_reported_and_resolves_to_the_first() -> None:
    index, duplicated = prerequisites.index_by_name(
        [("a", "Limits"), ("b", "Limits"), ("c", "Series")]
    )
    assert duplicated == ["Limits"]
    resolved, _ = prerequisites.resolve("c", ["Limits"], index)
    assert resolved == ["a"]


def test_a_two_node_cycle_loses_its_closing_edge() -> None:
    kept, dropped = prerequisites.acyclic([("a", "b"), ("b", "a")])
    assert kept == [("a", "b")]
    assert dropped == [("b", "a")]


def test_a_long_cycle_is_caught_too() -> None:
    # Edge creation already refused self-loops; a three-node ring went straight through.
    kept, dropped = prerequisites.acyclic([("a", "b"), ("b", "c"), ("c", "a")])
    assert kept == [("a", "b"), ("b", "c")]
    assert dropped == [("c", "a")]


def test_dropping_a_cycle_keeps_every_edge_it_can() -> None:
    kept, dropped = prerequisites.acyclic([("a", "b"), ("b", "c"), ("c", "a"), ("a", "d")])
    assert ("a", "d") in kept
    assert len(dropped) == 1


def test_the_result_is_a_function_of_the_input_order_alone() -> None:
    edges = [("a", "b"), ("b", "c"), ("c", "a")]
    assert prerequisites.acyclic(edges) == prerequisites.acyclic(list(edges))


def test_a_kept_graph_always_sorts_without_leftovers() -> None:
    """What the whole exercise is for: topo_sort tolerates a cycle by appending the leftovers
    in tiebreak order, so a stored cycle does not look like a failure — it looks like an
    order, and the learner is taught in it."""
    kept, _ = prerequisites.acyclic([("a", "b"), ("b", "c"), ("c", "a")])
    ids = {name: uuid.uuid4() for name in ("a", "b", "c")}
    edges = [Edge(prereq_kc_id=ids[p], kc_id=ids[k]) for p, k in kept]
    order = topo_sort(list(ids.values()), edges, {v: n for n, v in ids.items()})
    positions = {node: i for i, node in enumerate(order)}
    assert all(positions[ids[p]] < positions[ids[k]] for p, k in kept)


# --- parsing ----------------------------------------------------------------


def _curriculum_json(kcs_block: str) -> str:
    return (
        '{"subject_name": "Linear Algebra", "subject_description": "d", '
        '"topics": [{"name": "Foundations", "description": "d", "kcs": [' + kcs_block + "]}]}"
    )


async def _parse(reply: str):
    client = LLMClient(
        {"fake": FakeProvider(reply=reply)},
        {r: ModelSpec("fake", "fake-1") for r in ModelRole},
    )
    proposal, _usage = await generate_curriculum(client, goal="g", materials=None)
    return proposal


async def test_parsing_resolves_prerequisites_to_keys() -> None:
    proposal = await _parse(
        _curriculum_json(
            '{"name": "Vectors", "description": "d", "requires": []},'
            '{"name": "Bases", "description": "d", "requires": ["Vectors"]}'
        )
    )
    assert proposal is not None
    vectors, bases = proposal.topics[0].kcs
    assert vectors.requires == ()
    assert bases.requires == (vectors.key,)


async def test_parsing_resolves_a_prerequisite_in_a_later_topic() -> None:
    reply = (
        '{"subject_name": "S", "subject_description": "d", "topics": ['
        '{"name": "T1", "description": "d", "kcs": ['
        '{"name": "Advanced", "description": "d", "requires": ["Basic"]}]},'
        '{"name": "T2", "description": "d", "kcs": ['
        '{"name": "Basic", "description": "d", "requires": []}]}]}'
    )
    proposal = await _parse(reply)
    assert proposal is not None
    advanced = proposal.topics[0].kcs[0]
    basic = proposal.topics[1].kcs[0]
    assert advanced.requires == (basic.key,)


async def test_parsing_drops_a_cycle_rather_than_rejecting_the_curriculum() -> None:
    proposal = await _parse(
        _curriculum_json(
            '{"name": "A", "description": "d", "requires": ["B"]},'
            '{"name": "B", "description": "d", "requires": ["A"]}'
        )
    )
    assert proposal is not None, "a cycle must not cost the learner the whole curriculum"
    a, b = proposal.topics[0].kcs
    assert (a.requires, b.requires) in [((), (a.key,)), ((b.key,), ())]


async def test_parsing_survives_a_missing_requires_field() -> None:
    # Everything generated before this existed, and any model that ignores the instruction.
    proposal = await _parse(_curriculum_json('{"name": "A", "description": "d"}'))
    assert proposal is not None
    assert proposal.topics[0].kcs[0].requires == ()
    assert proposal.topics[0].kcs[0].key


# --- persistence ------------------------------------------------------------


async def _commit(session: AsyncSession, topics: list[dict]) -> uuid.UUID:
    result = await svc.create_subject_with_graph(
        session,
        subject_name=f"Subj {uuid.uuid4().hex[:6]}",
        subject_description="d",
        topics_data=topics,
        source_ids=None,
        learner_id=uuid.uuid4(),
    )
    return result.subject.id


async def _edges(session: AsyncSession, subject_id: uuid.UUID) -> list[tuple[str, str]]:
    rows = (await session.scalars(select(KCEdge))).all()
    names = {kc.id: kc.name for kc in (await session.scalars(select(KC))).all()}
    return sorted((names[e.prereq_kc_id], names[e.kc_id]) for e in rows)


async def test_committing_a_curriculum_persists_its_edges(db_session: AsyncSession) -> None:
    subject_id = await _commit(
        db_session,
        [
            {
                "name": "T",
                "description": "d",
                "kcs": [
                    {"name": "Vectors", "description": "d", "key": "t0k0", "requires": []},
                    {"name": "Bases", "description": "d", "key": "t0k1", "requires": ["t0k0"]},
                ],
            }
        ],
    )
    assert await _edges(db_session, subject_id) == [("Vectors", "Bases")]


async def test_an_edge_can_cross_topics(db_session: AsyncSession) -> None:
    subject_id = await _commit(
        db_session,
        [
            {
                "name": "T1",
                "description": "d",
                "kcs": [{"name": "Later", "description": "d", "key": "t0k0", "requires": ["t1k0"]}],
            },
            {
                "name": "T2",
                "description": "d",
                "kcs": [{"name": "Earlier", "description": "d", "key": "t1k0", "requires": []}],
            },
        ],
    )
    assert await _edges(db_session, subject_id) == [("Earlier", "Later")]


async def test_a_client_edited_payload_cannot_store_a_cycle(db_session: AsyncSession) -> None:
    """Parsing de-cycles the model's output; this is a request body, which a client is free to
    have edited in between. A stored cycle corrupts every plan built from the subject."""
    subject_id = await _commit(
        db_session,
        [
            {
                "name": "T",
                "description": "d",
                "kcs": [
                    {"name": "A", "description": "d", "key": "a", "requires": ["b"]},
                    {"name": "B", "description": "d", "key": "b", "requires": ["a"]},
                ],
            }
        ],
    )
    assert len(await _edges(db_session, subject_id)) == 1


async def test_a_dangling_reference_does_not_fail_the_commit(db_session: AsyncSession) -> None:
    # What a learner deleting a KC in the review step leaves behind.
    subject_id = await _commit(
        db_session,
        [
            {
                "name": "T",
                "description": "d",
                "kcs": [{"name": "A", "description": "d", "key": "a", "requires": ["deleted"]}],
            }
        ],
    )
    assert await _edges(db_session, subject_id) == []


async def test_a_payload_without_keys_still_commits(db_session: AsyncSession) -> None:
    # A hand-built subject, and everything created before S22 existed.
    subject_id = await _commit(
        db_session,
        [{"name": "T", "description": "d", "kcs": [{"name": "A", "description": "d"}]}],
    )
    assert await _edges(db_session, subject_id) == []


async def test_a_hand_written_payload_may_name_prerequisites(db_session: AsyncSession) -> None:
    subject_id = await _commit(
        db_session,
        [
            {
                "name": "T",
                "description": "d",
                "kcs": [
                    {"name": "Vectors", "description": "d", "key": "k0"},
                    {"name": "Bases", "description": "d", "key": "k1", "requires": ["Vectors"]},
                ],
            }
        ],
    )
    assert await _edges(db_session, subject_id) == [("Vectors", "Bases")]


@pytest.mark.parametrize("requires", [["a"], ["a", "a"]])
async def test_a_self_edge_is_never_stored(db_session: AsyncSession, requires: list[str]) -> None:
    # The database rejects it outright, so letting one through fails the whole commit.
    subject_id = await _commit(
        db_session,
        [
            {
                "name": "T",
                "description": "d",
                "kcs": [{"name": "A", "description": "d", "key": "a", "requires": requires}],
            }
        ],
    )
    assert await _edges(db_session, subject_id) == []
