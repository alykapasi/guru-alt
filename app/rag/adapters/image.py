"""Image adapter — vision-LLM OCR of a picture (handwritten/scanned notes, screenshots).

One image becomes one located unit whose text is the model's transcription. Needs the LLM
client from the extraction context; the VISION call's cost is recorded on the context for
the pipeline to log.
"""

from app.llm import ModelRole
from app.rag.adapters.base import ExtractContext, ExtractedUnit
from app.rag.ocr import ocr_image


class ImageOcrAdapter:
    name = "ocr"

    def handles(self, content_type: str) -> bool:
        return content_type.startswith("image/")

    async def extract(self, data: bytes, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        if ctx.llm is None:
            raise ValueError("image OCR requires an LLM client in the extraction context")
        text, usage = await ocr_image(ctx.llm, data, media_type=ctx.media_type)
        ctx.record_usage(ModelRole.VISION, usage)
        if not text.strip():
            return []
        return [ExtractedUnit(text=text, locator={"image": ctx.origin})]
