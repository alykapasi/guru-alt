"""Real Ollama integration test — opt-in via GURU_LIVE_MODEL_TESTS=1.

Exercises the OpenAICompatProvider against a locally running Ollama, satisfying the
Phase 2 DoD ("an Ollama integration test exercises real local generation"). Skipped by
default so the ordinary suite stays offline — see tests/live_models.py.
"""

import pytest

from app.core.config import get_settings
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.types import ChatMessage, ChatRole
from tests.live_models import SKIP_REASON, pick_model

_OLLAMA = get_settings().ollama_base_url

# Prefer a recognizable instruct/chat model; skip embedding and custom/fine-tuned models
# (which may not respond to a generic chat prompt).
_CHAT_PREFIXES = (
    "phi3",
    "phi4",
    "llama3",
    "qwen",
    "mistral",
    "gemma3",
    "granite",
    "openhermes",
    "deepseek",
)

_MODEL = pick_model(_CHAT_PREFIXES)


@pytest.mark.skipif(_MODEL is None, reason=SKIP_REASON)
async def test_ollama_streaming_real() -> None:
    assert _MODEL is not None  # narrow for the type checker; skipif guarantees it
    provider = OpenAICompatProvider(
        name="ollama", base_url=_OLLAMA, api_key="", timeout=30.0, max_retries=0
    )
    chunks = [
        chunk
        async for chunk in provider.stream(
            model=_MODEL,
            messages=[ChatMessage(role=ChatRole.USER, content="Reply with one word.")],
            max_tokens=16,
        )
    ]
    text = "".join(c.text for c in chunks if c.text)
    assert text.strip(), "expected non-empty generation from Ollama"
