"""Provider-agnostic message, usage, and role types."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelRole(StrEnum):
    """Semantic capability tiers. Map to concrete models in config, not in code."""

    FAST = "fast"  # tagging, routing, classification, the refinement gate
    SMART = "smart"  # tutoring, grading, most generation
    GENIUS = "genius"  # hard reasoning, curriculum/graph synthesis
    VISION = "vision"  # image understanding / OCR — must map to a multimodal model
    EMBED = "embed"  # vectorization


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"  # carries a ToolResultPart back to the model


class TextPart(BaseModel):
    """A text span within a multimodal message."""

    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """An inline image within a multimodal message (raw bytes + IANA media type)."""

    type: Literal["image"] = "image"
    media_type: str  # e.g. "image/png", "image/jpeg"
    data: bytes


class ToolUsePart(BaseModel):
    """An assistant's request to call a tool. ``id`` ties it to the eventual ToolResultPart."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultPart(BaseModel):
    """A tool's result, carried back to the model on a ``ChatRole.TOOL`` message."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentPart = TextPart | ImagePart | ToolUsePart | ToolResultPart
"""One part of a multimodal message. Providers translate to their native shape."""


class ChatMessage(BaseModel):
    role: ChatRole
    # A plain string (the common case) or ordered multimodal parts (text + images + tool parts).
    content: str | list[ContentPart]


class ToolDef(BaseModel):
    """A tool offered to the model. Providers translate ``parameters`` to their native schema."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for the tool's input


class ToolCall(BaseModel):
    """One tool invocation the model requested."""

    id: str
    name: str
    input: dict[str, Any]


def text_of(content: str | list[ContentPart]) -> str:
    """The text of a message's content, ignoring images (usage counting, text extraction)."""
    if isinstance(content, str):
        return content
    return " ".join(p.text for p in content if isinstance(p, TextPart))


class Usage(BaseModel):
    """Token accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ChatChunk(BaseModel):
    """One streamed delta. The terminal chunk may carry final ``usage``/``tool_calls``."""

    text: str = ""
    usage: Usage | None = None
    tool_calls: list[ToolCall] | None = None  # None = not yet known; set on the terminal chunk


class ChatResponse(BaseModel):
    """A complete (non-streamed) completion."""

    content: str
    usage: Usage
    model: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
