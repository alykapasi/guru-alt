"""Adapter registry — dispatch a content type to the adapter that handles it."""

from app.rag.adapters.audio import AudioAdapter
from app.rag.adapters.base import Adapter, ExtractContext, ExtractedUnit
from app.rag.adapters.docx import DocxAdapter
from app.rag.adapters.epub import EpubAdapter
from app.rag.adapters.html import HtmlAdapter
from app.rag.adapters.image import ImageOcrAdapter
from app.rag.adapters.pdf import PdfAdapter
from app.rag.adapters.pptx import PptxAdapter
from app.rag.adapters.text import TextAdapter
from app.rag.adapters.video import VideoAdapter
from app.rag.adapters.xlsx import XlsxAdapter

# Order matters only if content-type ranges overlap; today they don't.
_ADAPTERS: list[Adapter] = [
    TextAdapter(),
    PdfAdapter(),
    DocxAdapter(),
    PptxAdapter(),
    XlsxAdapter(),
    HtmlAdapter(),
    EpubAdapter(),
    AudioAdapter(),
    VideoAdapter(),
    ImageOcrAdapter(),
]


def select_adapter(content_type: str) -> Adapter | None:
    """The adapter for ``content_type`` (bare type, no parameters), or ``None``."""
    base = content_type.split(";", 1)[0].strip().lower()
    for adapter in _ADAPTERS:
        if adapter.handles(base):
            return adapter
    return None


def register_adapter(adapter: Adapter) -> None:
    """Add an adapter (used as later slices introduce doc/web extractors)."""
    _ADAPTERS.append(adapter)


__all__ = [
    "Adapter",
    "ExtractContext",
    "ExtractedUnit",
    "register_adapter",
    "select_adapter",
]
