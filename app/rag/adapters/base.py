"""Source adapters: turn raw bytes into located text units (TECHNICAL_DESIGN §6.1).

Each adapter handles one family of content types and extracts coarse **units** (a PDF
page, a slide, the whole text file) carrying a provenance ``locator``. Chunking then
subdivides units; the pipeline embeds and stores. Adapters are pure + synchronous (CPU
extraction); any I/O (fetching a URL) is the pipeline's job, so every adapter takes bytes.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ExtractedUnit(BaseModel):
    """A span of extracted text plus where it came from (page/slide/url/…)."""

    text: str
    locator: dict = Field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    """Extracts located text units from one content-type family."""

    name: str  # recorded as the provenance extraction method

    def handles(self, content_type: str) -> bool:
        """Whether this adapter can extract the given content type."""
        ...

    def extract(self, data: bytes, *, meta: dict) -> list[ExtractedUnit]:
        """Extract located text units from ``data``."""
        ...
