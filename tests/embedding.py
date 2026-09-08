"""The embedding space the fake client writes and reads in.

Vectors are only compared within one space (see ``app/llm/embedding_space.py``), so a test
that seeds a chunk or memory has to say which space its vector belongs to — the same one the
fake-backed retrieval under test will compute, or the row is correctly ignored.
"""

from app.core.config import get_settings
from app.llm.embedding_space import current_space
from app.llm.registry import fake_llm_client

FAKE_SPACE = current_space(fake_llm_client(), dim=get_settings().embed_dim)
