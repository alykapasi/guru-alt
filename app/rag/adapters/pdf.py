"""PDF adapter — one located unit per page (PyMuPDF).

Born-digital pages yield their extracted text. A page with (almost) no extractable text is
treated as **scanned**: if the context has an LLM, the page is rendered to an image and
transcribed by vision-OCR (recorded with ``method="ocr"`` so provenance stays honest).
"""

from collections.abc import Awaitable
from pathlib import Path

import pymupdf

from app.llm import ModelRole
from app.rag.adapters.base import ExtractContext, ExtractedUnit
from app.rag.concurrency import gather_bounded
from app.rag.ocr import ocr_image

_MIN_TEXT_CHARS = 16
"""Below this many extracted characters, a page is treated as scanned and OCR'd."""

_OCR_DPI = 200
"""Render resolution for scanned pages — high enough for legible OCR."""


class PdfAdapter:
    name = "pdf"

    def handles(self, content_type: str) -> bool:
        return content_type == "application/pdf"

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        # Born-digital pages resolve inline; scanned pages become OCR coroutines fanned out with
        # bounded concurrency. A slot per page preserves page order regardless of finish order.
        with pymupdf.open(str(path), filetype="pdf") as doc:
            slots: list[ExtractedUnit | None] = [None] * doc.page_count
            ocr_coros: list[Awaitable[ExtractedUnit | None]] = []
            ocr_slots: list[int] = []
            for index, page in enumerate(doc):
                page_no = index + 1
                text = page.get_text()
                if len(text.strip()) >= _MIN_TEXT_CHARS:
                    slots[index] = ExtractedUnit(text=text, locator={"page": page_no})
                elif ctx.llm is not None:
                    ocr_coros.append(self._ocr_page(page, page_no, ctx))
                    ocr_slots.append(index)
                elif text.strip():  # a little text, but no OCR capability — keep what we have
                    slots[index] = ExtractedUnit(text=text, locator={"page": page_no})
            if ocr_coros:
                for index, unit in zip(
                    ocr_slots, await gather_bounded(ocr_coros, ctx.ocr_concurrency), strict=True
                ):
                    slots[index] = unit
        return [unit for unit in slots if unit is not None]

    @staticmethod
    async def _ocr_page(
        page: pymupdf.Page, page_no: int, ctx: ExtractContext
    ) -> ExtractedUnit | None:
        """Render a scanned page to a PNG and transcribe it with the VISION model.

        Pixmap rendering is synchronous, so it never interleaves with another page's render
        (PyMuPDF stays single-threaded); only the vision call overlaps across pages.
        """
        assert ctx.llm is not None  # guarded by the caller
        image = page.get_pixmap(dpi=_OCR_DPI).tobytes("png")
        text, usage = await ocr_image(ctx.llm, image, media_type="image/png")
        ctx.record_usage(ModelRole.VISION, usage)
        if text.strip():
            return ExtractedUnit(text=text, locator={"page": page_no}, method="ocr")
        return None
