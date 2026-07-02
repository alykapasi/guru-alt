"""Source adapters: turn raw bytes into located text units (TECHNICAL_DESIGN §6.1).

Each adapter handles one family of content types and extracts coarse **units** (a PDF
page, a slide, the whole text file) carrying a provenance ``locator``. Chunking then
subdivides units; the pipeline embeds and stores.

Adapters are **async** and receive an :class:`ExtractContext` carrying the source descriptor
plus optional capabilities (an LLM client for vision-OCR, a transcriber for ASR, …). Plain
text/doc adapters ignore the context; modality adapters use it. Any I/O on the *source*
(fetching a URL) is still the pipeline's job — every adapter takes bytes.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.llm import LLMClient, ModelRole, Usage


class ExtractedUnit(BaseModel):
    """A span of extracted text plus where it came from (page/slide/url/timestamp/…)."""

    text: str
    locator: dict = Field(default_factory=dict)


@dataclass
class ExtractContext:
    """What an adapter needs beyond the raw bytes: source descriptor + capabilities.

    ``llm`` is present for adapters that call a model (vision-OCR). Adapters record any
    model usage via :meth:`record_usage` so the pipeline can log its cost.
    """

    content_type: str = ""
    origin: str = ""  # filename or URL — used as a provenance locator
    llm: LLMClient | None = None
    usage_log: list[tuple[ModelRole, Usage]] = field(default_factory=list)

    @property
    def media_type(self) -> str:
        """The bare content type (no parameters), usable as an image/audio media type."""
        return self.content_type.split(";", 1)[0].strip().lower()

    def record_usage(self, role: ModelRole, usage: Usage) -> None:
        """Record a model call made during extraction (for cost logging)."""
        self.usage_log.append((role, usage))


@runtime_checkable
class Adapter(Protocol):
    """Extracts located text units from one content-type family."""

    name: str  # recorded as the provenance extraction method

    def handles(self, content_type: str) -> bool:
        """Whether this adapter can extract the given content type."""
        ...

    async def extract(self, data: bytes, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        """Extract located text units from ``data``."""
        ...
