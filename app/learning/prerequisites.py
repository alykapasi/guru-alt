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
from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import TypeVar

_WHITESPACE = re.compile(r"\s+")

N = TypeVar("N", bound=Hashable)
"""A graph node. ``str`` proposal keys at parse time, ``uuid.UUID`` KC ids at plan time.

Spelled as a ``TypeVar`` rather than PEP 695 ``def acyclic[N]`` because beartype cannot
decorate the latter — it warns and skips, which would have quietly dropped runtime type
enforcement from the two functions below while everything still appeared to work."""

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


# Both suppress UP047: ruff would rather these used PEP 695 type parameters, which beartype
# then refuses to decorate — see N above. Runtime enforcement is the deliberate choice.
def _reaches(start: N, goal: N, successors: Mapping[N, list[N]]) -> bool:  # noqa: UP047
    """Whether ``goal`` is reachable from ``start`` following prereq → dependent edges."""
    stack = [start]
    seen: set[N] = set()
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(successors.get(node, ()))
    return False


def acyclic(  # noqa: UP047 — see N: beartype cannot decorate PEP 695 generics
    edges: Sequence[tuple[N, N]],
) -> tuple[list[tuple[N, N]], list[tuple[N, N]]]:
    """Keep the largest prefix-stable subset of ``edges`` that stays acyclic.

    Edges are considered in the order given and an edge is kept unless it closes a cycle
    against what has already been kept, which makes the outcome a deterministic function of
    the input rather than of dictionary ordering.

    The alternative — rejecting a curriculum containing a cycle — makes generated curricula
    less reliable than they are today, when they have no edges to be wrong about. This keeps
    what is usable; dropping the closing edge means the order is honestly unconstrained
    there, rather than an order the graph never justified.

    Generic over the node type because the same rule has to hold in two places and must not
    drift between them (S23): the curriculum parser applies it to proposal keys before
    anything is stored, and lesson-plan generation applies it to KC ids after loading a graph
    that may predate either check.
    """
    successors: dict[N, list[N]] = {}
    kept: list[tuple[N, N]] = []
    dropped: list[tuple[N, N]] = []
    for prereq, dependent in edges:
        # Adding prereq -> dependent closes a cycle exactly when dependent already reaches
        # prereq through the edges kept so far.
        if _reaches(dependent, prereq, successors):
            dropped.append((prereq, dependent))
            continue
        successors.setdefault(prereq, []).append(dependent)
        kept.append((prereq, dependent))
    return kept, dropped
