"""Image vision-OCR: the OCR primitive, the adapter, and the end-to-end ingest path.

The FakeProvider returns a canned reply for any message (including images), which simulates
a vision model's transcription. A live OCR test against a local Ollama vision model is
skipped when none is available (same pattern as the Ollama/rubric integration tests).
"""

import base64
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.registry import fake_llm_client
from app.models.chat import LLMCall
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind, SourceStatus
from app.rag.adapters import ExtractContext, select_adapter
from app.rag.adapters.image import ImageOcrAdapter
from app.rag.ocr import ocr_image
from app.services import ingestion
from app.storage import InMemoryBlobStore
from tests.live_models import SKIP_REASON, pick_model

# A 1x1 PNG — enough bytes to exercise the image path (and a real vision call).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# --- primitive + adapter ----------------------------------------------------


async def test_ocr_image_returns_text_and_usage() -> None:
    text, usage = await ocr_image(fake_llm_client("two plus two"), _PNG, media_type="image/png")
    assert text == "two plus two"
    assert usage.total_tokens > 0


async def test_image_adapter_extracts_ocr_unit(tmp_path: Path) -> None:
    img = tmp_path / "note.png"
    img.write_bytes(_PNG)
    ctx = ExtractContext(
        content_type="image/jpeg", origin="note.jpg", llm=fake_llm_client("my notes")
    )
    units = await ImageOcrAdapter().extract(img, meta={}, ctx=ctx)
    assert len(units) == 1
    assert units[0].text == "my notes"
    assert units[0].locator == {"image": "note.jpg"}
    assert len(ctx.usage_log) == 1  # the VISION call was recorded for cost logging


async def test_image_adapter_requires_llm(tmp_path: Path) -> None:
    img = tmp_path / "note.png"
    img.write_bytes(_PNG)
    with pytest.raises(ValueError, match="requires an LLM"):
        await ImageOcrAdapter().extract(img, meta={}, ctx=ExtractContext(content_type="image/png"))


def test_registry_dispatches_images_to_ocr() -> None:
    assert isinstance(select_adapter("image/png"), ImageOcrAdapter)
    assert isinstance(select_adapter("image/jpeg"), ImageOcrAdapter)


# --- end-to-end ingest ------------------------------------------------------


async def test_ingest_image_ocrs_and_logs_cost(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="handwriting.png",
        content_type="image/png",
        data=_PNG,
    )
    result = await ingestion.ingest_source(
        db_session, store, fake_llm_client("transcribed handwriting"), source.id
    )
    assert result.status == SourceStatus.DONE

    chunks = (await db_session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all()
    assert len(chunks) == 1
    assert "transcribed handwriting" in chunks[0].text
    assert chunks[0].provenance["method"] == "ocr"
    assert chunks[0].provenance["image"] == "handwriting.png"

    # The VISION call was cost-logged.
    calls = (await db_session.scalars(select(LLMCall).where(LLMCall.role == "vision"))).all()
    assert len(calls) == 1
    assert calls[0].learner_id == learner.id


# --- live vision model (opt-in via GURU_LIVE_MODEL_TESTS=1) ------------------

_OLLAMA = get_settings().ollama_base_url
_VISION_PREFIXES = ("llama3.2-vision", "llava", "moondream", "bakllava", "qwen2.5vl", "minicpm-v")

# A vision model is the slowest thing this repo can ask a laptop to load, which makes it the
# most important one to keep out of the default run.
_VISION_MODEL = pick_model(_VISION_PREFIXES, exclude=None)


@pytest.mark.skipif(_VISION_MODEL is None, reason=SKIP_REASON)
async def test_ocr_against_live_vision_model() -> None:
    from app.llm import ModelRole
    from app.llm.providers.openai_compat import OpenAICompatProvider
    from app.llm.registry import LLMClient, ModelSpec

    assert _VISION_MODEL is not None
    provider = OpenAICompatProvider(
        name="ollama", base_url=_OLLAMA, api_key="", timeout=30.0, max_retries=0
    )
    client = LLMClient({"ollama": provider}, {ModelRole.VISION: ModelSpec("ollama", _VISION_MODEL)})
    text, _usage = await ocr_image(client, _PNG, media_type="image/png")
    assert isinstance(text, str)  # a real vision model returns *some* description
