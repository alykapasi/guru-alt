"""Provider-agnostic message, usage, and role types."""

from enum import StrEnum

from pydantic import BaseModel


class ModelRole(StrEnum):
    """Semantic capability tiers. Map to concrete models in config, not in code."""

    FAST = "fast"  # tagging, routing, classification, the refinement gate
    SMART = "smart"  # tutoring, grading, most generation
    GENIUS = "genius"  # hard reasoning, curriculum/graph synthesis
    EMBED = "embed"  # vectorization


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    role: ChatRole
    content: str


class Usage(BaseModel):
    """Token accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ChatChunk(BaseModel):
    """One streamed delta. The terminal chunk may carry final ``usage``."""

    text: str = ""
    usage: Usage | None = None


class ChatResponse(BaseModel):
    """A complete (non-streamed) completion."""

    content: str
    usage: Usage
    model: str
