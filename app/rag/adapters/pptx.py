"""PPTX adapter — one located unit per slide (python-pptx)."""

import io

from pptx import Presentation

from app.rag.adapters.base import ExtractedUnit

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class PptxAdapter:
    name = "pptx"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    def extract(self, data: bytes, *, meta: dict) -> list[ExtractedUnit]:
        presentation = Presentation(io.BytesIO(data))
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
