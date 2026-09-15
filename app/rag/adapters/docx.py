"""DOCX adapter — one unit for the document (python-docx).

Word documents are flow text with no native page boundaries, so we join the blocks and let
chunking window the result; provenance records the method + char offsets.

**Tables are read, and until this they were not (S27).** ``document.paragraphs`` returns only
the body's top-level paragraphs; a paragraph inside a table cell is not among them. So a report
whose numbers live in tables — which is most reports — was ingested as its prose and nothing
else. Not mangled: absent. Ingestion reported success, the source went to ``done``, and the
figures the learner uploaded the document *for* were never indexed, with nothing anywhere
saying so.

Reading the body's children in order matters as much as reading them at all: a table dropped in
after its introduction belongs there, and appending all the tables at the end would put every
one of them under whatever paragraph happened to be last.
"""

from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.rag.adapters.base import ExtractContext, ExtractedUnit

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Tab-separated, because that is what survives normalization as a column boundary — see
# `app.rag.chunking._collapse`. A cell's own line breaks are flattened to spaces so that one
# row stays one line and the column count a reader sees is the column count the table had.
_COLUMN = "\t"


def _row_text(row_cells: list[str]) -> str:
    return _COLUMN.join(cell.replace("\n", " ").strip() for cell in row_cells)


def _table_text(table: Table) -> str:
    """One line per row, cells tab-separated, empty cells kept.

    Kept rather than skipped: dropping an empty cell shifts every column after it, so a row
    with a gap in the middle silently becomes a row with different columns.
    """
    rows = [_row_text([cell.text for cell in row.cells]) for row in table.rows]
    return "\n".join(row for row in rows if row.strip(_COLUMN).strip())


def _blocks(document: DocumentObject) -> list[str]:
    """Paragraph and table text in document order."""
    body = document.element.body
    out: list[str] = []
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = Paragraph(child, document).text
            if text.strip():
                out.append(text)
        elif tag == "tbl":
            text = _table_text(Table(child, document))
            if text.strip():
                out.append(text)
    return out


class DocxAdapter:
    name = "docx"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        document = Document(str(path))
        text = "\n".join(_blocks(document))
        return [ExtractedUnit(text=text)] if text.strip() else []
