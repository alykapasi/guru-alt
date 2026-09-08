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

from app.llm.registry import LLMClient
from app.llm.types import ModelRole


def current_space(llm: LLMClient, *, dim: int) -> str:
    """The space vectors written now belong to."""
    spec = llm.spec(ModelRole.EMBED)
    return f"{spec.provider}:{spec.model}:{dim}"
