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
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from app.learning.placement_inference import KCCandidate
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage

OBJECTIVE_ROLE = ModelRole.FAST
"""Objective selection is a cheap classification task — same tier as placement inference."""

StepType = Literal["new", "review"]
StepStatus = Literal["pending", "active", "done"]


class StepDict(TypedDict):
    """One plan step, JSON-serializable (``kc_id`` is a ``str``, not a ``uuid.UUID``)."""

    kc_id: str
    order: int
    step_type: StepType
    status: StepStatus
    target_difficulty: float | None
    hint_density: str | None
    preferred_item_type: str | None


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


def topo_sort(
    kc_ids: Iterable[uuid.UUID],
    edges: Sequence[Edge],
    tiebreak: Mapping[uuid.UUID, Any],
) -> list[uuid.UUID]:
    """Kahn's algorithm over ``kc_ids``, ties broken by ``tiebreak`` (e.g. ``(topic.slug,
    kc.slug)``). Edges touching a KC outside ``kc_ids`` are ignored — the caller is expected
    to pass a closed set (see :func:`prerequisite_closure`).

    A cycle shouldn't crash plan generation: any KC left over once no more nodes are ready
    is appended in ``tiebreak`` order rather than raising.
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
        return tiebreak.get(n, n)

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
        ordered.extend(sorted(nodes.difference(ordered), key=_key))
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
