"""DOCX adapter — the whole document as one unit (python-docx).

Word documents are flow text with no native page boundaries, so we join paragraphs and let
chunking window the result; provenance records the method + char offsets.
"""

import io

from docx import Document

from app.rag.adapters.base import ExtractedUnit

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DocxAdapter:
    name = "docx"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    def extract(self, data: bytes, *, meta: dict) -> list[ExtractedUnit]:
        document = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
        return [ExtractedUnit(text=text)] if text.strip() else []
