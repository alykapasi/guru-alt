"""PDF adapter — one located unit per page (PyMuPDF)."""

import pymupdf

from app.rag.adapters.base import ExtractContext, ExtractedUnit


class PdfAdapter:
    name = "pdf"

    def handles(self, content_type: str) -> bool:
        return content_type == "application/pdf"

    async def extract(self, data: bytes, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        units: list[ExtractedUnit] = []
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                text = page.get_text()
                if text.strip():
                    units.append(ExtractedUnit(text=text, locator={"page": index + 1}))
        return units
