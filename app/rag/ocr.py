"""Vision-LLM OCR primitive (TECHNICAL_DESIGN §6.1).

One reusable function that transcribes an image with the VISION model. Used by the image
adapter, the scanned-PDF fallback, and video keyframes. All model access is by role — no
provider SDK here, no hardcoded model. The caller logs cost from the returned ``Usage``.
"""

from app.llm import ChatMessage, ChatRole, ImagePart, LLMClient, ModelRole, TextPart, Usage

OCR_ROLE = ModelRole.VISION
"""Image transcription runs on the VISION tier (must resolve to a multimodal model)."""

_DEFAULT_PROMPT = (
    "Transcribe all text in this image exactly, preserving reading order and structure. "
    "If the image has no text, briefly describe what it depicts. Output only the transcription."
)


async def ocr_image(
    llm: LLMClient,
    image: bytes,
    *,
    media_type: str,
    prompt: str = _DEFAULT_PROMPT,
    max_tokens: int = 1024,
) -> tuple[str, Usage]:
    """Transcribe ``image`` with the VISION model. Returns the text plus the call's ``Usage``."""
    message = ChatMessage(
        role=ChatRole.USER,
        content=[TextPart(text=prompt), ImagePart(media_type=media_type, data=image)],
    )
    response = await llm.complete(OCR_ROLE, [message], max_tokens=max_tokens)
    return response.content.strip(), response.usage
