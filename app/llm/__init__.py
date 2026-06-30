"""Provider-agnostic LLM layer.

Application code talks to :class:`~app.llm.registry.LLMClient` by **role**
(`FAST`/`SMART`/`GENIUS`/`EMBED`); the registry resolves each role to a concrete
`(provider, model)` from settings. No service or router imports a provider SDK directly.
"""

from app.llm.registry import LLMClient, build_llm_client
from app.llm.types import ChatChunk, ChatMessage, ChatResponse, ChatRole, ModelRole, Usage

__all__ = [
    "ChatChunk",
    "ChatMessage",
    "ChatResponse",
    "ChatRole",
    "LLMClient",
    "ModelRole",
    "Usage",
    "build_llm_client",
]
