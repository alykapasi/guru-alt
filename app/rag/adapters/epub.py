"""EPUB adapter — one located unit per spine chapter (ebooklib).

An EPUB is a zip of XHTML documents ordered by a *spine*. We walk the spine (reading order),
skip the navigation document, and emit one unit per content chapter with a
``{chapter, title}`` locator. Chapter bodies are XHTML, so text is pulled with lxml (the same
tree tool the HTML path relies on) rather than readability extraction, which would over-strip
short chapters. Opened from a path so large books never sit in memory as one blob.
"""

from pathlib import Path

import ebooklib
from ebooklib import epub
from lxml import html as lxml_html

from app.rag.adapters.base import ExtractContext, ExtractedUnit

_CONTENT_TYPE = "application/epub+zip"


class EpubAdapter:
    name = "epub"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        # ignore_ncx: read the spine/nav from the OPF only (silences ebooklib's NCX FutureWarning).
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
        units: list[ExtractedUnit] = []
        chapter_no = 0
        for idref, _linear in book.spine:
            item = book.get_item_with_id(idref)
            if item is None or isinstance(item, epub.EpubNav):
                continue  # skip the table-of-contents navigation document
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            tree = lxml_html.fromstring(item.get_content())
            text = tree.text_content()
            if not text.strip():
                continue
            chapter_no += 1
            title = _chapter_title(tree) or Path(item.get_name()).stem
            units.append(ExtractedUnit(text=text, locator={"chapter": chapter_no, "title": title}))
        return units


def _chapter_title(tree: lxml_html.HtmlElement) -> str:
    """The chapter's heading (first ``h1``/``h2``) or its ``<title>``; empty if none."""
    for xpath in (".//h1", ".//h2", ".//title"):
        heading = tree.findtext(xpath)
        if heading and heading.strip():
            return heading.strip()
    return ""
