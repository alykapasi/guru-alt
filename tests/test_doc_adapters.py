"""Per-format doc adapters: extraction + locators, and PDF dispatch through the pipeline."""

import io
import uuid

import pymupdf
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind, SourceStatus
from app.rag.adapters import select_adapter
from app.rag.adapters.docx import DocxAdapter
from app.rag.adapters.pdf import PdfAdapter
from app.rag.adapters.pptx import PptxAdapter
from app.rag.adapters.xlsx import XlsxAdapter
from app.services import ingestion
from app.storage import InMemoryBlobStore

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- fixture builders (generate real files in-memory) -----------------------


def _pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _docx(paragraphs: list[str]) -> bytes:
    document = Document()
    for para in paragraphs:
        document.add_paragraph(para)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _pptx(slides: list[str]) -> bytes:
    prs = Presentation()
    blank = prs.slide_layouts[6]
    for text in slides:
        slide = prs.slides.add_slide(blank)
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2))
        box.text_frame.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _xlsx(title: str, rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- extraction -------------------------------------------------------------


def test_pdf_adapter_extracts_one_unit_per_page() -> None:
    units = PdfAdapter().extract(_pdf(["Hello from page one", "Second page content"]), meta={})
    assert len(units) == 2
    assert units[0].locator == {"page": 1} and "page one" in units[0].text
    assert units[1].locator == {"page": 2} and "Second page" in units[1].text


def test_docx_adapter_joins_paragraphs() -> None:
    units = DocxAdapter().extract(_docx(["First paragraph.", "Second paragraph."]), meta={})
    assert len(units) == 1
    assert "First paragraph" in units[0].text and "Second paragraph" in units[0].text


def test_pptx_adapter_extracts_one_unit_per_slide() -> None:
    units = PptxAdapter().extract(_pptx(["Slide one bullet", "Slide two bullet"]), meta={})
    assert len(units) == 2
    assert units[0].locator == {"slide": 1} and "Slide one" in units[0].text
    assert units[1].locator == {"slide": 2}


def test_xlsx_adapter_extracts_one_unit_per_sheet() -> None:
    units = XlsxAdapter().extract(_xlsx("Scores", [["Name", "Score"], ["Alice", 90]]), meta={})
    assert len(units) == 1
    assert units[0].locator == {"sheet": "Scores"}
    assert "Alice" in units[0].text and "90" in units[0].text


def test_registry_dispatches_by_content_type() -> None:
    assert isinstance(select_adapter("application/pdf"), PdfAdapter)
    assert isinstance(select_adapter(DOCX_CT), DocxAdapter)
    assert isinstance(select_adapter(PPTX_CT), PptxAdapter)
    assert isinstance(select_adapter(XLSX_CT), XlsxAdapter)
    assert select_adapter("application/x-unknown") is None


# --- pipeline dispatch ------------------------------------------------------


async def test_ingest_pdf_records_page_provenance(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    data = _pdf(["Photosynthesis occurs in chloroplasts.", "Respiration occurs in mitochondria."])
    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="biology.pdf",
        content_type="application/pdf",
        data=data,
    )
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    assert result.status == SourceStatus.DONE

    chunks = (
        await db_session.scalars(
            select(Chunk).where(Chunk.source_id == source.id).order_by(Chunk.ordinal)
        )
    ).all()
    assert len(chunks) >= 2
    assert chunks[0].provenance["method"] == "pdf"
    assert {c.provenance["page"] for c in chunks} == {1, 2}
