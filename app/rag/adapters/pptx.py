"""PPTX adapter — one located unit per slide (python-pptx)."""

from pathlib import Path

from pptx import Presentation

from app.rag.adapters.base import ExtractContext, ExtractedUnit

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class PptxAdapter:
    name = "pptx"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        presentation = Presentation(str(path))
        units: list[ExtractedUnit] = []
        for index, slide in enumerate(presentation.slides):
            lines = [
                shape.text_frame.text
                for shape in slide.shapes
                if shape.has_text_frame and shape.text_frame.text.strip()
            ]
            text = "\n".join(lines)
            if text.strip():
                units.append(ExtractedUnit(text=text, locator={"slide": index + 1}))
        return units
