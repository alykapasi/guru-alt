"""Plain-text adapter — the whole file is one unit; chunking does the splitting."""

from app.rag.adapters.base import ExtractedUnit


class TextAdapter:
    name = "text"

    def handles(self, content_type: str) -> bool:
        return content_type.startswith("text/plain") or content_type in {"", "text/markdown"}

    def extract(self, data: bytes, *, meta: dict) -> list[ExtractedUnit]:
        text = data.decode("utf-8", errors="replace")
        return [ExtractedUnit(text=text)] if text.strip() else []
