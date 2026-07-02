"""DOCX adapter — the whole document as one unit (python-docx).

Word documents are flow text with no native page boundaries, so we join paragraphs and let
chunking window the result; provenance records the method + char offsets.
"""

from pathlib import Path

from docx import Document

from app.rag.adapters.base import ExtractContext, ExtractedUnit

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DocxAdapter:
    name = "docx"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        document = Document(str(path))
        text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
        return [ExtractedUnit(text=text)] if text.strip() else []
