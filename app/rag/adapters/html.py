"""HTML adapter — readability extraction of a page's main content (trafilatura).

Strips nav/boilerplate, keeping the article text. The source URL (passed via ``meta``)
becomes the provenance locator for citations.
"""

import trafilatura

from app.rag.adapters.base import ExtractContext, ExtractedUnit


class HtmlAdapter:
    name = "html"

    def handles(self, content_type: str) -> bool:
        return content_type in {"text/html", "application/xhtml+xml"}

    async def extract(self, data: bytes, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        text = trafilatura.extract(data.decode("utf-8", errors="replace")) or ""
        if not text.strip():
            return []
        url = meta.get("url")
        locator = {"url": url} if url else {}
        return [ExtractedUnit(text=text, locator=locator)]
