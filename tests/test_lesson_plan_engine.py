"""Lesson plan: pure graph/policy layer (``app/learning/lesson_plan.py``)."""

import json
import uuid

from app.learning.lesson_plan import (
    Edge,
    build_initial_steps,
    prerequisite_closure,
    select_objectives,
    topo_sort,
)
from app.learning.placement_inference import KCCandidate
from app.llm.registry import fake_llm_client


def _uuids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


# --- prerequisite_closure ---------------------------------------------------


def test_prerequisite_closure_walks_transitively() -> None:
    a, b, c, d = _uuids(4)
    # Diamond: A -> B -> D, A -> C -> D.
    edges = [
        Edge(prereq_kc_id=a, kc_id=b),
        Edge(prereq_kc_id=a, kc_id=c),
        Edge(prereq_kc_id=b, kc_id=d),
        Edge(prereq_kc_id=c, kc_id=d),
    ]
    assert prerequisite_closure([d], edges) == {a, b, c, d}


def test_prerequisite_closure_includes_targets_with_no_prereqs() -> None:
    a, isolated = _uuids(2)
    edges = [Edge(prereq_kc_id=a, kc_id=uuid.uuid4())]
    assert prerequisite_closure([isolated], edges) == {isolated}


# --- topo_sort ---------------------------------------------------------------


def test_topo_sort_orders_diamond_by_prerequisite_then_tiebreak() -> None:
    a, b, c, d, e = _uuids(5)
    edges = [
        Edge(prereq_kc_id=a, kc_id=b),
        Edge(prereq_kc_id=a, kc_id=c),
        Edge(prereq_kc_id=b, kc_id=d),
        Edge(prereq_kc_id=c, kc_id=d),
    ]
    tiebreak = {a: "a", b: "b", c: "c", d: "d", e: "e"}  # e is an isolated node
    order = topo_sort([a, b, c, d, e], edges, tiebreak)

    assert order.index(a) < order.index(b) < order.index(d)
    assert order.index(a) < order.index(c) < order.index(d)
    assert set(order) == {a, b, c, d, e}
    assert order == [a, b, c, d, e]  # deterministic given this tiebreak


def test_topo_sort_ignores_edges_outside_the_node_set() -> None:
    a, b, outside = _uuids(3)
    edges = [Edge(prereq_kc_id=outside, kc_id=a), Edge(prereq_kc_id=a, kc_id=b)]
    order = topo_sort([a, b], edges, {a: "a", b: "b"})
    assert order == [a, b]


def test_topo_sort_cycle_falls_back_to_tiebreak_order_instead_of_raising() -> None:
    x, y = _uuids(2)
    edges = [Edge(prereq_kc_id=x, kc_id=y), Edge(prereq_kc_id=y, kc_id=x)]
    order = topo_sort([x, y], edges, {x: "x", y: "y"})
    assert set(order) == {x, y}
    assert order == [x, y]


# --- build_initial_steps ------------------------------------------------------


def test_build_initial_steps_is_a_bare_pending_skeleton_in_order() -> None:
    a, b = _uuids(2)
    steps = build_initial_steps([a, b])
    assert steps == [
        {
            "kc_id": str(a),
            "order": 0,
            "step_type": "new",
            "status": "pending",
            "target_difficulty": None,
            "hint_density": None,
            "preferred_item_type": None,
        },
        {
            "kc_id": str(b),
            "order": 1,
            "step_type": "new",
            "status": "pending",
            "target_difficulty": None,
            "hint_density": None,
            "preferred_item_type": None,
        },
    ]


# --- select_objectives ---------------------------------------------------------


def _candidates(n: int) -> list[KCCandidate]:
    return [KCCandidate(id=uuid.uuid4(), name=f"KC {i}") for i in range(n)]


async def test_select_objectives_no_candidates_skips_the_call() -> None:
    kcs, usage = await select_objectives(fake_llm_client("irrelevant"), "goal", [])
    assert kcs == []
    assert usage.total_tokens == 0


async def test_select_objectives_parses_valid_reply() -> None:
    candidates = _candidates(3)
    reply = json.dumps({"kcs": [2]})
    kcs, usage = await select_objectives(fake_llm_client(reply), "learn topic 2", candidates)
    assert kcs == [candidates[1].id]
    assert usage.total_tokens > 0


async def test_select_objectives_dedupes_and_preserves_first_occurrence_order() -> None:
    candidates = _candidates(3)
    reply = json.dumps({"kcs": [3, 1, 3]})
    kcs, _ = await select_objectives(fake_llm_client(reply), "goal", candidates)
    assert kcs == [candidates[2].id, candidates[0].id]


async def test_select_objectives_out_of_range_indices_are_dropped() -> None:
    candidates = _candidates(2)
    reply = json.dumps({"kcs": [0, 5, 1]})
    kcs, _ = await select_objectives(fake_llm_client(reply), "goal", candidates)
    assert kcs == [candidates[0].id]


async def test_select_objectives_garbage_reply_yields_empty_list() -> None:
    candidates = _candidates(2)
    kcs, _ = await select_objectives(fake_llm_client("not json at all"), "goal", candidates)
    assert kcs == []


async def test_select_objectives_empty_kcs_list_is_valid() -> None:
    candidates = _candidates(2)
    reply = json.dumps({"kcs": []})
    kcs, _ = await select_objectives(fake_llm_client(reply), "vague goal", candidates)
    assert kcs == []
