"""Turning a model's proposed prerequisites into a graph the planner can actually order by.

A generated curriculum arrives as topics and KCs plus, per KC, the names of the things it
says should come first. Three things can be wrong with that list, and all three are the
model's ordinary failure modes rather than exceptional ones: it can name a KC that is not in
the proposal, it can name the KC itself, and it can describe a cycle.

None of them is a reason to throw the curriculum away. A subject with most of its edges is
strictly better than the status quo, which is a subject with none at all — so every function
here *drops* what it cannot use and reports it, and none of them raise.

Pure: no database, no model calls. :mod:`app.services.knowledge` persists the result.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

_WHITESPACE = re.compile(r"\s+")

KeyEdge = tuple[str, str]
"""``(prereq_key, kc_key)`` — "the first should be mastered before the second"."""


def normalise(name: str) -> str:
    """The form two KC names are compared in: case-folded, whitespace collapsed.

    Deliberately loose. The model writes prerequisites by name, in prose it generated
    separately from the name itself, so "Vector Spaces" and "vector spaces" are the same
    concept and matching them exactly would drop the edge for a capital letter.
    """
    return _WHITESPACE.sub(" ", name).strip().casefold()


def index_by_name(named_keys: Sequence[tuple[str, str]]) -> tuple[dict[str, str], list[str]]:
    """Map each KC's normalised name to its key; first occurrence wins.

    Returns the index and the names that appeared more than once. A repeated name is not an
    error here — it means "these two KCs cannot be told apart by name", so a prerequisite
    naming it is ambiguous and resolves to whichever came first. Establishing that two
    identically named KCs are the same concept is S24's question, not this module's.
    """
    index: dict[str, str] = {}
    duplicated: list[str] = []
    for key, name in named_keys:
        norm = normalise(name)
        if not norm:
            continue
        if norm in index:
            duplicated.append(name)
            continue
        index[norm] = key
    return index, duplicated


def resolve(
    key: str, required_names: Iterable[str], index: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """Resolve one KC's prerequisite names to keys.

    Returns ``(resolved, unresolved)``. A name the proposal does not contain is dropped: the
    model reaching outside the curriculum it just wrote is exactly the case a foreign-key
    error would turn into a failed commit. A KC naming itself is dropped the same way — the
    database rejects that edge anyway, and one bad entry should not fail the insert.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for name in required_names:
        if not isinstance(name, str):
            continue
        target = index.get(normalise(name))
        if target is None:
            unresolved.append(name)
            continue
        if target == key or target in seen:
            continue
        seen.add(target)
        resolved.append(target)
    return resolved, unresolved


def _reaches(start: str, goal: str, successors: Mapping[str, list[str]]) -> bool:
    """Whether ``goal`` is reachable from ``start`` following prereq → dependent edges."""
    stack = [start]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(successors.get(node, ()))
    return False


def acyclic(edges: Sequence[KeyEdge]) -> tuple[list[KeyEdge], list[KeyEdge]]:
    """Keep the largest prefix-stable subset of ``edges`` that stays acyclic.

    Edges are considered in the order given and an edge is kept unless it closes a cycle
    against what has already been kept, which makes the outcome a deterministic function of
    the proposal rather than of dictionary ordering.

    The alternative — rejecting a curriculum containing a cycle — makes generated curricula
    less reliable than they are today, when they have no edges to be wrong about. This keeps
    what is usable. ``topo_sort`` already tolerates a cycle by appending the leftovers, but
    tolerating one there means the learner is silently given an order the graph did not
    justify; dropping the edge here means the order is honestly unconstrained instead.
    """
    successors: dict[str, list[str]] = {}
    kept: list[KeyEdge] = []
    dropped: list[KeyEdge] = []
    for prereq, dependent in edges:
        # Adding prereq -> dependent closes a cycle exactly when dependent already reaches
        # prereq through the edges kept so far.
        if _reaches(dependent, prereq, successors):
            dropped.append((prereq, dependent))
            continue
        successors.setdefault(prereq, []).append(dependent)
        kept.append((prereq, dependent))
    return kept, dropped
