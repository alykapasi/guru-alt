"""Source adapters: turn raw bytes into located text units (TECHNICAL_DESIGN §6.1).

Each adapter handles one family of content types and extracts coarse **units** (a PDF
page, a slide, the whole text file) carrying a provenance ``locator``. Chunking then
subdivides units; the pipeline embeds and stores.

Adapters are **async** and receive the source as a **local file path** (the pipeline streams
the blob to a temp file, so even huge files never sit in memory — PDF/EPUB open lazily) plus an
:class:`ExtractContext` carrying the source descriptor and optional capabilities (an LLM client
for vision-OCR, a transcriber for ASR, …). Plain text/doc adapters ignore the context; modality
adapters use it.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.llm import LLMClient, ModelRole, Usage
from app.rag.demux import MediaDemuxer
from app.rag.transcription import Transcriber

# Tab, because that is the separator normalization keeps as a column boundary rather than
# collapsing into the surrounding whitespace (see `app.rag.chunking._collapse`).
COLUMN = "\t"


def table_text(rows: Iterable[Iterable[str]]) -> str:
    """A table as one line per row, cells tab-separated (S27).

    Shared by every adapter that meets a table, because they all have the same two ways to
    destroy one and it would be three separate chances to get it wrong. An empty cell is kept
    as an empty field: dropping it moves every later value one column left, so a row with a gap
    in the middle silently becomes a row of different data rather than a row with a gap. The line
    breaks inside a cell become spaces, so one row stays one line — otherwise the row ends early
    and the remainder of the cell becomes a line with no columns at all. A wholly blank row is
    dropped, which displaces nothing.
    """
    lines = [COLUMN.join(" ".join(cell.split()) for cell in row) for row in rows]
    return "\n".join(line for line in lines if line.strip(COLUMN).strip())


class ExtractedUnit(BaseModel):
    """A span of extracted text plus where it came from (page/slide/url/timestamp/…).

    ``method`` overrides the adapter's default extraction method for this unit — used when a
    single adapter mixes methods (e.g. a PDF with born-digital *and* OCR'd pages, or a video
    with ASR transcript *and* keyframe-OCR units). ``None`` means "use the adapter's name".
    """

    text: str
    locator: dict = Field(default_factory=dict)
    method: str | None = None


@dataclass
class ExtractContext:
    """What an adapter needs beyond the raw bytes: source descriptor + capabilities.

    ``llm`` is present for adapters that call a model (vision-OCR); ``transcriber`` for adapters
    that do speech-to-text (audio/video); ``demuxer`` for video (splits it into an audio track +
    keyframes). Adapters record any model usage via :meth:`record_usage` so the pipeline can log
    its cost. ``ocr_concurrency`` caps how many vision-OCR calls an adapter runs at once (the
    pipeline sets it from config; the default matches ``Settings.ocr_concurrency``).
    """

    content_type: str = ""
    origin: str = ""  # filename or URL — used as a provenance locator
    llm: LLMClient | None = None
    transcriber: Transcriber | None = None
    demuxer: MediaDemuxer | None = None
    ocr_concurrency: int = 5
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

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        """Extract located text units from the file at ``path``."""
        ...
