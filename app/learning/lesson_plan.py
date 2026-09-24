"""Pure policy logic for the adaptive lesson plan (MASTERPLAN §4.6, TECHNICAL_DESIGN §7.7).

No DB access here — ``app/services/lesson_plan.py`` owns the I/O (candidate KCs, mastery,
profile, persistence) and converts ORM rows to the small decoupled types below (mirrors
``placement_inference.py``'s ``KCCandidate``). Keeping the graph algorithms and step-revision
policy pure makes them unit-testable in isolation from the database.
"""

import json
import uuid
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict

from app.learning import prerequisites as prereq_index
from app.learning.placement_inference import KCCandidate
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage

OBJECTIVE_ROLE = ModelRole.FAST
"""Objective selection is a cheap classification task — same tier as placement inference."""

StepType = Literal["new", "review", "detour"]

DETOUR_ITEM_TYPE = "short"
"""What a detour step asks with, whatever the learner's format preference says (S11).

A detour is a hypothesis — "the reason you cannot do this is that you cannot do that yet" —
and ordinary practice on the prerequisite does not test it. An open question does: it produces
a failure kind and a per-component split on the *prerequisite*, so the answer says whether the
prerequisite is genuinely missing or whether the detour was a wrong turn. A format that cannot
say why an answer failed cannot test a claim about why an answer failed.

It deliberately overrides ``score_by_format``. That dimension is about which formats a learner
does well on, which is the wrong question to ask of a step whose purpose is to find something
out."""
StepStatus = Literal["pending", "active", "done", "proposed", "skipped"]
Guidance = Literal["guided", "exploration"]
DetourOutcome = Literal["mastered", "disproved", "skipped"]

OPEN_DETOUR_STATUSES: frozenset[str] = frozenset({"pending", "active", "proposed"})
"""A detour step still in play. `proposed` counts: offering the same prerequisite twice while
the learner is still deciding on the first offer would be the planner not listening."""


class DetourNotOpen(Exception):
    """A decision named a detour that is not open, or accepted one that was never offered."""


DETOUR_DIAGNOSED = "diagnosed"
DETOUR_REPEATED_FAILURE = "repeated_failure"
"""Why a detour was taken. The first is the grader saying so outright (S09); the second is the
learner failing the same component repeatedly with a prerequisite still unmastered — no
diagnosis needed, which is what keeps detours reachable from objective items."""


class StepDict(TypedDict):
    """One plan step, JSON-serializable (``kc_id`` is a ``str``, not a ``uuid.UUID``)."""

    kc_id: str
    order: int
    step_type: StepType
    status: StepStatus
    target_difficulty: float | None
    hint_density: str | None
    preferred_item_type: str | None
    # Set only on a "detour" step: the component the learner was actually trying to reach.
    # NotRequired because every plan written before detours existed has no such key, and a
    # revision must not have to migrate a plan to read it.
    detour_for: NotRequired[str | None]
    detour_reason: NotRequired[str | None]
    # When the learner actually started the detour (guided: on insertion; exploration: on
    # acceptance). Disproval only counts evidence from after it, so an old pass cannot close
    # a new detour. Absent on detours written before this existed — those are never disproved.
    opened_at: NotRequired[str | None]
    detour_outcome: NotRequired[DetourOutcome | None]


@dataclass(frozen=True)
class Detour:
    """A prerequisite to send the learner to before the component they are stuck on (S11)."""

    prereq_kc_id: uuid.UUID
    blocked_kc_id: uuid.UUID
    reason: str
    # How long the run of failures was when this was decided. Carried so the event log can
    # record how stuck the learner actually was, rather than only that a detour happened —
    # "detoured after two misses" and "detoured after six" are different situations and a
    # later look at whether detours help needs to be able to tell them apart.
    consecutive_failures: int = 0


def prerequisite_detour(
    *,
    blocked_kc_id: uuid.UUID,
    prerequisites: Sequence[uuid.UUID],
    prerequisite_names: Mapping[uuid.UUID, str],
    mastered: Iterable[uuid.UUID],
    diagnosed_name: str = "",
    consecutive_failures: int = 0,
    min_failures: int = 2,
) -> Detour | None:
    """Which prerequisite, if any, is worth investigating before the blocked component.

    Two independent triggers, deliberately. The grader naming a prerequisite (S09) is direct
    evidence and acts on a single answer — being told the failure is upstream is the whole
    point of asking. Repeated failure with an unmastered prerequisite is the behavioural
    fallback, and it exists because most generated items are MCQs, which produce no diagnosis
    at all; without it detours would only ever fire on open questions.

    Only *direct* prerequisites are considered. Jumping three levels back on one bad answer
    is not a teaching decision anyone would defend, and it is not needed: if the prerequisite
    the learner detours to is itself blocked, the next failure detours again, one level at a
    time.

    A diagnosed name that matches nothing here is dropped. The grader sees the question, not
    the graph, so it can name something true and irrelevant — or something that is not a
    component at all.
    """
    done = set(mastered)
    candidates = [kc_id for kc_id in prerequisites if kc_id not in done]
    if not candidates:
        return None  # nothing upstream is outstanding, so the difficulty is here
    if diagnosed_name:
        index, _ = prereq_index.index_by_name(
            [(str(kc_id), prerequisite_names.get(kc_id, "")) for kc_id in candidates]
        )
        target = index.get(prereq_index.normalise(diagnosed_name))
        if target is not None:
            return Detour(
                prereq_kc_id=uuid.UUID(target),
                blocked_kc_id=blocked_kc_id,
                reason=DETOUR_DIAGNOSED,
                consecutive_failures=consecutive_failures,
            )
    if consecutive_failures >= min_failures:
        # The first in prerequisite order: nearest to where they already are.
        return Detour(
            prereq_kc_id=candidates[0],
            blocked_kc_id=blocked_kc_id,
            reason=DETOUR_REPEATED_FAILURE,
            consecutive_failures=consecutive_failures,
        )
    return None


@dataclass(frozen=True)
class Edge:
    """A prerequisite edge, decoupled from the ORM ``KCEdge``: mastering ``prereq_kc_id``
    should precede ``kc_id``."""

    prereq_kc_id: uuid.UUID
    kc_id: uuid.UUID


def prerequisite_closure(target_ids: Iterable[uuid.UUID], edges: Sequence[Edge]) -> set[uuid.UUID]:
    """Every target KC plus all of its transitive prerequisites (BFS backward over ``edges``)."""
    prereqs_of: dict[uuid.UUID, list[uuid.UUID]] = {}
    for edge in edges:
        prereqs_of.setdefault(edge.kc_id, []).append(edge.prereq_kc_id)

    closure: set[uuid.UUID] = set()
    queue: deque[uuid.UUID] = deque(target_ids)
    while queue:
        kc_id = queue.popleft()
        if kc_id in closure:
            continue
        closure.add(kc_id)
        queue.extend(prereqs_of.get(kc_id, []))
    return closure


class CyclicPrerequisites(Exception):
    """A prerequisite graph containing a cycle was handed to :func:`topo_sort` (S23).

    Raised rather than worked around. The leftovers used to be appended in tiebreak order,
    which does not fail — it produces *an order*, and the learner is then taught in it. A
    prerequisite cycle means at least one component is scheduled before something it was
    declared to depend on, and nothing downstream can tell that apart from a real ordering.

    Callers decide what to do about a cycle *before* asking for an order:
    ``prerequisites.acyclic`` drops the closing edges and reports them, which leaves the
    remaining order genuinely justified by the remaining edges. That is the difference between
    "we could not honour this constraint" and silently pretending there was none.
    """

    def __init__(self, unordered: Sequence[uuid.UUID]) -> None:
        self.unordered = list(unordered)
        super().__init__(
            f"{len(self.unordered)} knowledge components are in a prerequisite cycle: "
            + ", ".join(str(kc_id) for kc_id in self.unordered)
        )


def topo_sort(
    kc_ids: Iterable[uuid.UUID],
    edges: Sequence[Edge],
    tiebreak: Mapping[uuid.UUID, Any],
) -> list[uuid.UUID]:
    """Kahn's algorithm over ``kc_ids``, ties broken by ``tiebreak`` (e.g. ``(topic.slug,
    kc.slug)``). Edges touching a KC outside ``kc_ids`` are ignored — the caller is expected
    to pass a closed set (see :func:`prerequisite_closure`).

    Raises :class:`CyclicPrerequisites` if the edges do not admit an order. Run the graph
    through ``prerequisites.acyclic`` first if it might not.
    """
    nodes = set(kc_ids)
    indegree: dict[uuid.UUID, int] = dict.fromkeys(nodes, 0)
    successors: dict[uuid.UUID, list[uuid.UUID]] = {n: [] for n in nodes}
    for edge in edges:
        if edge.kc_id not in nodes or edge.prereq_kc_id not in nodes:
            continue
        successors[edge.prereq_kc_id].append(edge.kc_id)
        indegree[edge.kc_id] += 1

    def _key(n: uuid.UUID) -> Any:
        """Total, and the same shape for every node — which the fallback used not to be.

        ``tiebreak`` maps this subject's KCs to integers, so returning the bare UUID for a KC
        it does not cover produced a list mixing ints and UUIDs, and sorting that raises
        ``TypeError``. The docstring above promises these are tolerated, so a cross-subject
        prerequisite reaching here did not order the plan badly — it stopped the plan existing
        (S24). Unknown nodes now sort after known ones, deterministically by id.
        """
        known = tiebreak.get(n)
        return (0, known, "") if known is not None else (1, 0, str(n))

    ready = sorted((n for n in nodes if indegree[n] == 0), key=_key)
    ordered: list[uuid.UUID] = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        newly_ready = []
        for succ in successors[node]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                newly_ready.append(succ)
        ready.extend(newly_ready)
        ready.sort(key=_key)

    if len(ordered) < len(nodes):
        raise CyclicPrerequisites(sorted(nodes.difference(ordered), key=_key))
    return ordered


def build_initial_steps(kc_order: Sequence[uuid.UUID]) -> list[StepDict]:
    """Bare step skeleton in topo order — no mastery/review/scaffolding awareness yet.

    Callers immediately run this through ``revise_steps`` to fill in status/hints; kept as a
    separate step so generation and every later revision share exactly the same status/order
    logic rather than duplicating it.
    """
    return [
        StepDict(
            kc_id=str(kc_id),
            order=i,
            step_type="new",
            status="pending",
            target_difficulty=None,
            hint_density=None,
            preferred_item_type=None,
        )
        for i, kc_id in enumerate(kc_order)
    ]


_OBJECTIVE_SYSTEM_PROMPT = (
    "A learner stated a goal for a subject. You are given a numbered list of candidate "
    "knowledge components in that subject. Select only the KCs that are the direct target of "
    "the learner's goal — not their prerequisites, which are handled separately. Respond with "
    'ONLY a JSON object {"kcs": [<candidate number>, ...]} and nothing else. If the goal '
    "doesn't clearly point at specific KCs, return an empty list."
)


async def select_objectives(
    client: LLMClient,
    goal: str,
    candidates: Sequence[KCCandidate],
    *,
    max_tokens: int = 512,
) -> tuple[list[uuid.UUID], Usage]:
    """Pick the KC(s) a learner's goal is actually about, from a numbered candidate list.

    No candidates, or a malformed/empty reply, ⇒ no objectives — best-effort; the caller
    falls back to targeting every KC in the subject rather than failing plan generation.
    """
    if not candidates:
        return [], Usage()
    completion = await client.complete(
        OBJECTIVE_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_objective_prompt(goal, candidates))],
        system=_OBJECTIVE_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    return _parse_objectives(completion.content, candidates), completion.usage


def _build_objective_prompt(goal: str, candidates: Sequence[KCCandidate]) -> str:
    catalog = "\n".join(
        f"{i}. {kc.name}{f' — {kc.description}' if kc.description else ''}"
        for i, kc in enumerate(candidates, start=1)
    )
    return f"Candidate KCs:\n{catalog}\n\nLearner's goal:\n{goal}"


def _parse_objectives(content: str, candidates: Sequence[KCCandidate]) -> list[uuid.UUID]:
    try:
        raw = json.loads(_extract_json(content))["kcs"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    selected: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for entry in raw:
        try:
            index = int(entry)
        except (TypeError, ValueError):
            continue
        if not 1 <= index <= len(candidates):
            continue
        kc_id = candidates[index - 1].id
        if kc_id not in seen:
            seen.add(kc_id)
            selected.append(kc_id)
    return selected


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]


# --- Profile-driven scaffolding ----------------------------------------------

HELP_SEEKING_LOW = 0.5
HELP_SEEKING_HIGH = 1.5
PERSISTENCE_HIGH = 0.6
"""v1-arbitrary thresholds on the profile_estimators value scales, same spirit as placement's
level->estimate mapping — not calibrated, revisit once real data exists."""


@dataclass(frozen=True)
class ScaffoldingHints:
    """Profile-driven teaching parameters. Every field defaults to a neutral value — a
    dimension that hasn't been computed yet never blocks plan generation."""

    target_difficulty: float | None = None
    hint_density: str | None = None  # "low" | "medium" | "high"
    preferred_item_type: str | None = None
    pacing: str = "standard"  # "brisk" | "standard" | "unhurried"
    example_tags: list[str] = field(default_factory=list)


# A format wins only if it wins on score *without* having been asked easier questions, and by
# enough that the difference is not noise. Both are uncalibrated v1 heuristics, in the same
# spirit as the placement mappings — the point is the shape of the rule, not these numbers.
FORMAT_DIFFICULTY_BAND = 0.1
FORMAT_SCORE_MARGIN = 0.1


def _preferred_item_type(score_by_format: Any) -> str | None:
    """The format to try first, or ``None`` when the scores do not justify choosing one (S44).

    This used to be ``max(mean_score)``. Formats are not matched on difficulty or topic, so
    the highest mean belongs to whichever format happened to ask the easiest questions — and
    routing a learner to it is a recommendation to practise the thing they already find easy,
    made on evidence that says nothing of the kind. A format now has to hold its own on
    difficulty and win by a margin, and where neither holds this returns nothing and the
    step's own default stands.

    An immediate score is still the wrong measure here — later retention and transfer are the
    ones that matter — so this narrows a bad rule rather than establishing a good one.
    """
    if not isinstance(score_by_format, dict):
        return None
    stats = {
        t: v
        for t, v in score_by_format.items()
        if isinstance(v, dict)
        and isinstance(v.get("mean_score"), int | float)
        and isinstance(v.get("mean_difficulty"), int | float)
    }
    if len(stats) < 2:
        return None  # nothing to compare against, so nothing to prefer
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["mean_score"], reverse=True)
    (best_key, best), (_, runner_up) = ranked[0], ranked[1]
    if best["mean_score"] - runner_up["mean_score"] < FORMAT_SCORE_MARGIN:
        return None  # too close to call
    hardest = max(v["mean_difficulty"] for v in stats.values())
    if best["mean_difficulty"] < hardest - FORMAT_DIFFICULTY_BAND:
        return None  # it only leads because its questions were easier
    return str(best_key)


def _hint_density(help_seeking: Any, persistence: Any) -> str | None:
    if not isinstance(help_seeking, int | float):
        return None
    persistent = isinstance(persistence, int | float) and persistence >= PERSISTENCE_HIGH
    if help_seeking <= HELP_SEEKING_LOW and persistent:
        return "low"
    if help_seeking >= HELP_SEEKING_HIGH and not persistent:
        return "high"
    return "medium"


def scaffolding_from_profile(values: Mapping[str, Any]) -> ScaffoldingHints:
    """Map a learner's current profile dimension values to teaching parameters.

    ``values`` is ``{dimension_key: value}`` — the unwrapped ``ProfileDimension.value``s from
    ``profile.get_snapshot``, keyed by dimension key. A dimension not present (or shaped
    unexpectedly) simply leaves its corresponding hint at the neutral default.
    """
    target_difficulty = (
        float(values["optimal_challenge"])
        if isinstance(values.get("optimal_challenge"), int | float)
        else None
    )

    hint_density = _hint_density(values.get("help_seeking"), values.get("persistence"))

    preferred_item_type = _preferred_item_type(values.get("score_by_format"))

    pacing = "standard"
    pace = values.get("pace")
    if isinstance(pace, dict):
        trend = pace.get("trend")
        if trend == "speeding_up":
            pacing = "brisk"
        elif trend == "slowing_down":
            pacing = "unhurried"

    interests = values.get("interests")
    example_tags = [str(t) for t in interests] if isinstance(interests, list) else []

    # No reading-level hint. It was a readability score of the learner's own chat messages,
    # used to instruct generation to write at that level — so short, casual questions asked a
    # tutor to simplify its explanations (S44). Presentation level belongs to an explicit
    # learner preference, which does not exist yet; an unjustified inference is worse than none.
    return ScaffoldingHints(
        target_difficulty=target_difficulty,
        hint_density=hint_density,
        preferred_item_type=preferred_item_type,
        pacing=pacing,
        example_tags=example_tags,
    )


# --- Step revision -------------------------------------------------------------


def horizon_extension(
    objective_kc_ids: Sequence[str],
    steps: Sequence[StepDict],
    *,
    max_open_steps: int,
) -> list[str]:
    """The next objective KCs to pull into the plan, in objective order.

    The step cap bounds how much work is *in front of* the learner at once, not how much of
    their goal they are allowed to reach. As steps finish, room opens and the next components
    of the objective move in — so a goal larger than the cap continues through to its actual
    target instead of ending at the twentieth prerequisite.

    Returns ``[]`` for a plan with no recorded objective (generated before objectives were
    stored), which leaves its behaviour exactly as it was.
    """
    planned = {step["kc_id"] for step in steps if step["step_type"] == "new"}
    open_steps = sum(1 for step in steps if step["step_type"] == "new" and step["status"] != "done")
    room = max_open_steps - open_steps
    if room <= 0:
        return []
    return [kc_id for kc_id in objective_kc_ids if kc_id not in planned][:room]


def revise_steps(
    steps: Sequence[StepDict],
    *,
    mastered_kc_ids: Iterable[uuid.UUID],
    due_review_kc_ids: Sequence[uuid.UUID],
    scaffolding: ScaffoldingHints,
    detour: Detour | None = None,
    guidance: Guidance = "guided",
    disproved_kc_ids: Iterable[uuid.UUID] = (),
    now: datetime | None = None,
) -> list[StepDict]:
    """Re-derive status/order/hints over an existing step list. Pure, no DB, no LLM — this is
    what makes revision cheap enough to run on every graded answer and profile refresh.

    ``due_review_kc_ids`` must already be ordered soonest-due first (as
    ``mastery.due_reviews`` returns them) — that order becomes the review-step ordering.

    0. A ``detour`` (S11) is inserted ahead of everything as its own step, unless one for that
       prerequisite is already open (``OPEN_DETOUR_STATUSES``). Under guided guidance it is
       inserted taken — ``status="pending"``, ``opened_at`` stamped now — and sorts before due
       reviews: a detour is the direct response to the failure that just happened, and putting
       a queue of flashcards between the two breaks that connection, while FSRS intervals are
       measured in days and tolerate a few minutes. Under exploration guidance it is only
       *proposed* (V07): ``status="proposed"``, no ``opened_at``, and it sorts immediately
       before the step it was proposed for rather than ahead of everything — the learner has
       not agreed to go yet, so nothing else in the plan moves for it.
    1. A ``"new"`` step whose KC is now mastered flips to ``"done"``. A ``"detour"`` step whose
       KC is now mastered flips to ``"done"`` with ``detour_outcome="mastered"`` — a proposal
       included, since a proposal is dropped once there is nothing left for it to test, and a
       proposal is never activated by mastery either (one-way ratchet — a KC that later needs
       review again gets a fresh review step, not an un-done "new" step).
    2. A taken (non-proposed) detour whose KC is in ``disproved_kc_ids`` *and* has a truthy
       ``opened_at`` flips to ``"done"`` with ``detour_outcome="disproved"`` — the grader's
       hypothesis was wrong, and the learner goes back to the step it was blocking. A detour
       with no ``opened_at`` (written before this existed) is never disproved, since there is
       no trustworthy start for "after" to mean anything, and a proposal is never disproved at
       all — it was never acted on, so there is nothing to disprove.
    3. A ``"proposed"`` detour whose blocked step (``detour_for``) is now ``"done"``, or is no
       longer in the plan at all, is dropped outright — no ``detour_outcome``, since an offer
       nobody answered is not a decision. A ``pending``/``active`` (guided) detour is untouched
       by this: it was already taken, not merely offered, so it runs its own course.
    4. An existing non-closed ``"review"`` step whose KC is no longer due flips to ``"done"``
       (it was reviewed, or the retention window passed). A due KC with no existing non-closed
       review step gets a new ``"pending"`` one.
    5. ``order`` is recomputed: an accepted (non-proposed) open detour first, then reviews
       (soonest-due first), then new steps and proposals (a proposal immediately before the
       new step it targets), then closed (``"done"``/``"skipped"``) steps last.
    6. ``active`` is recomputed: the first step whose status is not ``"done"``, ``"skipped"``
       or ``"proposed"`` — a proposal is never made active by revision; it only becomes active
       once the learner accepts it via ``decide_detour``.
    7. Scaffolding hints refresh on every step not ``"done"`` or ``"skipped"``; those keep the
       hints they were actually taught under.
    """
    result: list[StepDict] = [StepDict(**step) for step in steps]  # shallow per-step copy

    mastered = {str(kc_id) for kc_id in mastered_kc_ids}
    disproved = {str(kc_id) for kc_id in disproved_kc_ids}
    due_order = [str(kc_id) for kc_id in due_review_kc_ids]
    due_set = set(due_order)

    for step in result:
        if step["status"] in ("done", "skipped"):
            continue
        if step["step_type"] == "new":
            if step["kc_id"] in mastered:
                step["status"] = "done"
        elif step["step_type"] == "detour":
            if step["kc_id"] in mastered:
                step["status"], step["detour_outcome"] = "done", "mastered"
            elif (
                step["status"] != "proposed"
                and step["kc_id"] in disproved
                and step.get("opened_at")
            ):
                step["status"], step["detour_outcome"] = "done", "disproved"

    blocked_status = {
        step["kc_id"]: step["status"] for step in result if step["step_type"] == "new"
    }
    result = [
        step
        for step in result
        if not (
            step["step_type"] == "detour"
            and step["status"] == "proposed"
            and blocked_status.get(step.get("detour_for") or "", "done") == "done"
        )
    ]

    if detour is not None:
        prereq_id = str(detour.prereq_kc_id)
        already_open = any(
            step["step_type"] == "detour"
            and step["status"] in OPEN_DETOUR_STATUSES
            and step["kc_id"] == prereq_id
            for step in result
        )
        # A detour to a component the learner has already mastered would send them to work
        # they have demonstrated; the decision is made against mastery, but the plan may have
        # moved on since.
        if not already_open and prereq_id not in mastered:
            result.append(
                StepDict(
                    kc_id=prereq_id,
                    order=0,
                    step_type="detour",
                    status="proposed" if guidance == "exploration" else "pending",
                    target_difficulty=None,
                    hint_density=None,
                    preferred_item_type=None,
                    detour_for=str(detour.blocked_kc_id),
                    detour_reason=detour.reason,
                    opened_at=(
                        None
                        if guidance == "exploration"
                        else (now or datetime.now(UTC)).isoformat()
                    ),
                )
            )

    covered: set[str] = set()
    for step in result:
        if step["step_type"] != "review" or step["status"] in ("done", "skipped"):
            continue
        if step["kc_id"] not in due_set:
            step["status"] = "done"
        else:
            covered.add(step["kc_id"])

    for kc_id in due_order:
        if kc_id in covered:
            continue
        result.append(
            StepDict(
                kc_id=kc_id,
                order=0,
                step_type="review",
                status="pending",
                target_difficulty=None,
                hint_density=None,
                preferred_item_type=None,
            )
        )
        covered.add(kc_id)

    review_rank = {kc_id: i for i, kc_id in enumerate(due_order)}

    CLOSED = ("done", "skipped")
    blocked_order = {step["kc_id"]: step["order"] for step in result if step["step_type"] == "new"}

    def _bucket(step: StepDict) -> int:
        if step["status"] in CLOSED:
            return 3
        if step["step_type"] == "detour" and step["status"] != "proposed":
            return 0
        return 1 if step["step_type"] == "review" else 2

    def _within_bucket(step: StepDict) -> Any:
        if step["status"] not in CLOSED and step["step_type"] == "review":
            return review_rank.get(step["kc_id"], len(due_order))
        if step["status"] == "proposed":
            return (blocked_order.get(step.get("detour_for") or "", step["order"]), 0)
        if _bucket(step) == 2:
            return (step["order"], 1)
        return step["order"]

    result.sort(key=lambda s: (_bucket(s), _within_bucket(s)))
    for i, step in enumerate(result):
        step["order"] = i

    for step in result:
        if step["status"] == "active":
            step["status"] = "pending"
    for step in result:
        if step["status"] not in ("done", "skipped", "proposed"):
            step["status"] = "active"
            break

    for step in result:
        if step["status"] in CLOSED:
            continue
        step["target_difficulty"] = scaffolding.target_difficulty
        step["hint_density"] = scaffolding.hint_density
        step["preferred_item_type"] = (
            DETOUR_ITEM_TYPE if step["step_type"] == "detour" else scaffolding.preferred_item_type
        )

    return result


def decide_detour(
    steps: Sequence[StepDict],
    *,
    prereq_kc_id: uuid.UUID,
    decision: Literal["accept", "skip"],
    now: datetime,
) -> list[StepDict]:
    """Apply the learner's answer to a detour (S11). Pure; the caller re-runs ``revise_steps``.

    Accept is only for an offer; skip works on an offer or on a detour already under way, in
    either guidance mode (V07: both allow skipping).
    """
    result: list[StepDict] = [StepDict(**step) for step in steps]
    target = next(
        (
            step
            for step in result
            if step["step_type"] == "detour"
            and step["status"] in OPEN_DETOUR_STATUSES
            and step["kc_id"] == str(prereq_kc_id)
        ),
        None,
    )
    if target is None or (decision == "accept" and target["status"] != "proposed"):
        raise DetourNotOpen(str(prereq_kc_id))
    if decision == "accept":
        target["status"], target["opened_at"] = "pending", now.isoformat()
    else:
        target["status"], target["detour_outcome"] = "skipped", "skipped"
    return result
