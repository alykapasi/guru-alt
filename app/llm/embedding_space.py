"""Which vector space a stored embedding lives in.

Vectors are only comparable to other vectors produced by the same embedding model. Nothing
recorded which model produced a stored vector, so changing ``GURU_MODEL_EMBED`` to a
different model of the same dimension — a one-line config edit, no schema change, no error —
left the database full of vectors that cosine distance would happily rank against queries
from a space they have nothing to do with. The results would be wrong and would look normal:
plausible passages, confidently retrieved, unrelated to the question.

The identity is ``provider:model:dim``. Provider is part of it because the same model name
served by two backends is not a promise of the same weights, and dimension is part of it
because it is the one incompatibility that would otherwise surface as a database error rather
than as silently wrong ranking.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement

from app.llm.registry import LLMClient
from app.llm.types import ModelRole


def current_space(llm: LLMClient, *, dim: int) -> str:
    """The space vectors written now belong to."""
    spec = llm.spec(ModelRole.EMBED)
    return f"{spec.provider}:{spec.model}:{dim}"


def exact_cosine_distance(column: Any, vector: Sequence[float]) -> ColumnElement[float]:
    """Cosine distance from ``column`` to ``vector`` that the HNSW index cannot answer (S76).

    Every vector search here is scoped to one learner. Ordering by the bare ``column <=>
    vector`` lets the planner choose the HNSW index, which returns the ~40 nearest vectors in
    the *whole table* and only then applies the learner filter — so a learner whose rows are
    further from the query than other learners' rows gets some or none of them back, silently.
    Which plan it chose depended on table statistics, which is why this surfaced as an
    intermittent test failure that a ``VACUUM ANALYZE`` made disappear.

    Adding zero changes no value and matches no index operator, so the distance is computed
    exactly over the learner's own rows. That exact path is what was measured (50/50 against a
    sequential scan, about 4 µs per owned chunk); trading it for the index's speed is a
    separate, measured decision.
    """
    return column.cosine_distance(vector) + 0
