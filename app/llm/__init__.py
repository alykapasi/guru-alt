"""Provider-agnostic LLM layer.

Application code talks to :class:`~app.llm.registry.LLMClient` by **role**
(`FAST`/`SMART`/`GENIUS`/`EMBED`); the registry resolves each role to a concrete
`(provider, model)` from settings. No service or router imports a provider SDK directly.
"""

from app.llm.registry import LLMClient, build_llm_client
from app.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatResponse,
    ChatRole,
    ContentPart,
    EmbedResult,
    ImagePart,
    ModelRole,
    TextPart,
    ToolCall,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
    Usage,
    text_of,
)

__all__ = [
    "ChatChunk",
    "ChatMessage",
    "ChatResponse",
    "ChatRole",
    "ContentPart",
    "EmbedResult",
    "ImagePart",
    "LLMClient",
    "ModelRole",
    "TextPart",
    "ToolCall",
    "ToolDef",
    "ToolResultPart",
    "ToolUsePart",
    "Usage",
    "build_llm_client",
    "text_of",
]
