"""Lesson plan: pure graph/policy layer (``app/learning/lesson_plan.py``)."""

import json
import uuid

from app.learning import lesson_plan as engine
from app.learning.lesson_plan import (
    Edge,
    ScaffoldingHints,
    StepDict,
    build_initial_steps,
    prerequisite_closure,
    revise_steps,
    scaffolding_from_profile,
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


# --- scaffolding_from_profile ---------------------------------------------------


def test_scaffolding_from_profile_empty_snapshot_is_all_defaults() -> None:
    hints = scaffolding_from_profile({})
    assert hints == ScaffoldingHints()


def test_scaffolding_from_profile_maps_every_dimension() -> None:
    values = {
        "optimal_challenge": 0.62,
        "help_seeking": 0.3,
        "persistence": 0.8,
        "format_effectiveness": {
            "mcq": {"mean_score": 0.7, "mean_difficulty": 0.5, "n": 5},
            "cloze": {"mean_score": 0.9, "mean_difficulty": 0.5, "n": 5},
        },
        "pace": {"median_seconds": 12.0, "trend": "speeding_up"},
        "interests": ["basketball", "cooking"],
        "reading_level": 8.5,
    }
    hints = scaffolding_from_profile(values)
    assert hints.target_difficulty == 0.62
    assert hints.hint_density == "low"  # low help-seeking + high persistence
    assert hints.preferred_item_type == "cloze"  # higher mean_score
    assert hints.pacing == "brisk"
    assert hints.example_tags == ["basketball", "cooking"]
    assert hints.reading_level_hint == 8.5


def test_scaffolding_from_profile_partial_snapshot_defaults_the_rest() -> None:
    hints = scaffolding_from_profile({"optimal_challenge": 0.5})
    assert hints.target_difficulty == 0.5
    assert hints.hint_density is None
    assert hints.preferred_item_type is None
    assert hints.pacing == "standard"
    assert hints.example_tags == []
    assert hints.reading_level_hint is None


def test_scaffolding_from_profile_hint_density_high_for_high_help_seeking_low_persistence() -> None:
    hints = scaffolding_from_profile({"help_seeking": 2.0, "persistence": 0.1})
    assert hints.hint_density == "high"


def test_scaffolding_from_profile_hint_density_medium_for_ambiguous_combo() -> None:
    hints = scaffolding_from_profile({"help_seeking": 0.3, "persistence": 0.1})
    assert hints.hint_density == "medium"


def test_scaffolding_from_profile_pace_slowing_down_maps_to_unhurried() -> None:
    hints = scaffolding_from_profile({"pace": {"median_seconds": 30.0, "trend": "slowing_down"}})
    assert hints.pacing == "unhurried"


# --- revise_steps ----------------------------------------------------------------


def _step(kc_id: uuid.UUID, order: int, *, step_type="new", status="pending") -> StepDict:
    return StepDict(
        kc_id=str(kc_id),
        order=order,
        step_type=step_type,
        status=status,
        target_difficulty=None,
        hint_density=None,
        preferred_item_type=None,
    )


def test_revise_steps_marks_mastered_new_step_done_and_advances_active() -> None:
    a, b = _uuids(2)
    steps = [_step(a, 0, status="active"), _step(b, 1, status="pending")]
    result = revise_steps(
        steps, mastered_kc_ids={a}, due_review_kc_ids=[], scaffolding=ScaffoldingHints()
    )
    by_kc = {s["kc_id"]: s for s in result}
    assert by_kc[str(a)]["status"] == "done"
    assert by_kc[str(b)]["status"] == "active"


def test_revise_steps_mastery_ratchet_does_not_un_mark_done() -> None:
    a = uuid.uuid4()
    steps = [_step(a, 0, status="active")]
    once = revise_steps(
        steps, mastered_kc_ids={a}, due_review_kc_ids=[], scaffolding=ScaffoldingHints()
    )
    assert once[0]["status"] == "done"
    # kc_id no longer reported mastered — done must not revert.
    twice = revise_steps(
        once, mastered_kc_ids=set(), due_review_kc_ids=[], scaffolding=ScaffoldingHints()
    )
    assert twice[0]["status"] == "done"


def test_revise_steps_inserts_due_review_ahead_of_new_steps() -> None:
    new_kc, review_kc = _uuids(2)
    steps = [_step(new_kc, 0, status="active")]
    result = revise_steps(
        steps,
        mastered_kc_ids=set(),
        due_review_kc_ids=[review_kc],
        scaffolding=ScaffoldingHints(),
    )
    assert [s["kc_id"] for s in result] == [str(review_kc), str(new_kc)]
    assert result[0]["step_type"] == "review"
    assert result[0]["status"] == "active"  # first non-done step
    assert result[1]["status"] == "pending"  # demoted from active


def test_revise_steps_satisfied_review_becomes_done_and_is_not_reinserted() -> None:
    kc = uuid.uuid4()
    steps = [_step(kc, 0, step_type="review", status="active")]
    result = revise_steps(
        steps, mastered_kc_ids=set(), due_review_kc_ids=[], scaffolding=ScaffoldingHints()
    )
    assert len(result) == 1
    assert result[0]["status"] == "done"

    # Re-running with the KC due again inserts a *fresh* review step (history preserved).
    again = revise_steps(
        result, mastered_kc_ids=set(), due_review_kc_ids=[kc], scaffolding=ScaffoldingHints()
    )
    assert len(again) == 2
    statuses = sorted(s["status"] for s in again)
    assert statuses == ["active", "done"]


def test_revise_steps_orders_reviews_soonest_due_first() -> None:
    soon, later, new_kc = _uuids(3)
    steps = [_step(new_kc, 0, status="active")]
    result = revise_steps(
        steps,
        mastered_kc_ids=set(),
        due_review_kc_ids=[soon, later],  # ordered soonest-due first
        scaffolding=ScaffoldingHints(),
    )
    assert [s["kc_id"] for s in result[:2]] == [str(soon), str(later)]


def test_revise_steps_refreshes_scaffolding_on_non_done_but_not_done_steps() -> None:
    active_kc, done_kc = _uuids(2)
    steps = [
        _step(active_kc, 0, status="active"),
        StepDict(
            kc_id=str(done_kc),
            order=1,
            step_type="new",
            status="done",
            target_difficulty=0.1,
            hint_density="low",
            preferred_item_type="mcq",
        ),
    ]
    hints = ScaffoldingHints(
        target_difficulty=0.7, hint_density="high", preferred_item_type="cloze"
    )
    result = revise_steps(steps, mastered_kc_ids=set(), due_review_kc_ids=[], scaffolding=hints)
    by_kc = {s["kc_id"]: s for s in result}
    assert by_kc[str(active_kc)]["target_difficulty"] == 0.7
    assert by_kc[str(active_kc)]["hint_density"] == "high"
    assert by_kc[str(active_kc)]["preferred_item_type"] == "cloze"
    # Done step keeps its historical hints, untouched by the new scaffolding.
    assert by_kc[str(done_kc)]["target_difficulty"] == 0.1
    assert by_kc[str(done_kc)]["hint_density"] == "low"
    assert by_kc[str(done_kc)]["preferred_item_type"] == "mcq"


def test_revise_steps_fully_done_plan_has_no_active_step() -> None:
    kc = uuid.uuid4()
    steps = [_step(kc, 0, status="done")]
    result = revise_steps(
        steps, mastered_kc_ids=set(), due_review_kc_ids=[], scaffolding=ScaffoldingHints()
    )
    assert all(s["status"] != "active" for s in result)


# --- the step cap bounds the horizon, not the goal (S63) ----------------------


def _new_step(kc_id: str, status: str = "pending") -> engine.StepDict:
    return engine.StepDict(
        kc_id=kc_id,
        order=0,
        step_type="new",
        status=status,  # ty: ignore[invalid-argument-type]
        target_difficulty=None,
        hint_density=None,
        preferred_item_type=None,
    )


def test_a_full_horizon_pulls_in_nothing() -> None:
    objective = [f"kc-{i}" for i in range(5)]
    steps = [_new_step(kc) for kc in objective[:3]]
    assert engine.horizon_extension(objective, steps, max_open_steps=3) == []


def test_finished_work_makes_room_for_the_next_of_the_objective() -> None:
    objective = [f"kc-{i}" for i in range(5)]
    steps = [_new_step("kc-0", "done"), _new_step("kc-1"), _new_step("kc-2")]
    assert engine.horizon_extension(objective, steps, max_open_steps=3) == ["kc-3"]


def test_the_target_is_eventually_reached_however_deep_the_prerequisites() -> None:
    """The point of S63: the goal itself topo-sorts last and used to be cut off."""
    objective = [f"prereq-{i}" for i in range(24)] + ["the-actual-goal"]
    steps = [_new_step(kc) for kc in objective[:20]]
    assert "the-actual-goal" not in {s["kc_id"] for s in steps}

    for step in steps:  # the learner works through the first window
        step["status"] = "done"
    pulled = engine.horizon_extension(objective, steps, max_open_steps=20)
    assert pulled == objective[20:]
    assert "the-actual-goal" in pulled


def test_review_steps_do_not_occupy_the_horizon() -> None:
    """Retention work is added on top of the cap, so it must not crowd out new material."""
    objective = ["kc-0", "kc-1"]
    review = engine.StepDict(
        kc_id="kc-9",
        order=0,
        step_type="review",
        status="pending",
        target_difficulty=None,
        hint_density=None,
        preferred_item_type=None,
    )
    assert engine.horizon_extension(objective, [review], max_open_steps=1) == ["kc-0"]


def test_a_plan_with_no_recorded_objective_is_left_alone() -> None:
    """Plans generated before objectives existed keep behaving exactly as they did."""
    assert engine.horizon_extension([], [_new_step("kc-0", "done")], max_open_steps=20) == []
