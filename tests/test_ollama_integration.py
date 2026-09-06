"""Real Ollama integration test — skipped automatically when Ollama isn't available.

Exercises the OpenAICompatProvider against a locally running Ollama, satisfying the
Phase 2 DoD ("an Ollama integration test exercises real local generation"). Skipped in
CI and on machines without Ollama or any pulled model.
"""

import httpx
import pytest

from app.core.config import get_settings
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.types import ChatMessage, ChatRole

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


def _pick_chat_model() -> str | None:
    """Return a pulled chat-capable Ollama model id, or None if none/unreachable."""
    try:
        resp = httpx.get(f"{_OLLAMA}/models", timeout=2.0)
        resp.raise_for_status()
        ids = [m["id"] for m in resp.json().get("data", [])]
    except Exception:
        return None
    for prefix in _CHAT_PREFIXES:
        for model_id in ids:
            if model_id.startswith(prefix) and "embed" not in model_id:
                return model_id
    return None


_MODEL = _pick_chat_model()


@pytest.mark.skipif(_MODEL is None, reason="Ollama not running or no model pulled")
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
