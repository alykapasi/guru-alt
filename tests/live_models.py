"""Opt-in gate for the tests that call a real local model.

Most of the suite is offline and deterministic — LLM calls go through ``FakeProvider``. A
handful of tests deliberately do not: they exercise the OpenAI-compatible provider, the eval
rubric, and vision OCR against a locally running Ollama, which is the only way to prove those
paths work outside a fake.

Those tests used to run automatically whenever Ollama happened to be reachable, which made
``poe test`` mean something different on a laptop than in CI: a cold or busy model turned a
three-minute suite into a twenty-minute one with no indication why, and a model that answers
differently today turns a green suite red for reasons unrelated to the code. They are now
opt-in — ``GURU_LIVE_MODEL_TESTS=1 uv run poe test`` — so the default run is genuinely offline
and a green local run means a green CI run.
"""

import os
from collections.abc import Sequence

import httpx

from app.core.config import get_settings

LIVE_ENV_VAR = "GURU_LIVE_MODEL_TESTS"
SKIP_REASON = f"live-model test: set {LIVE_ENV_VAR}=1 (needs a local Ollama with the model)"

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get(LIVE_ENV_VAR, "").strip().lower() in _TRUTHY


def pick_model(prefixes: Sequence[str], *, exclude: str | None = "embed") -> str | None:
    """A pulled Ollama model whose id starts with one of ``prefixes``.

    ``None`` — so the caller skips — when live tests are off, Ollama is unreachable, or no
    matching model is pulled. The probe is short: an unreachable Ollama must not cost the
    suite anything.
    """
    if not enabled():
        return None
    try:
        resp = httpx.get(f"{get_settings().ollama_base_url}/models", timeout=2.0)
        resp.raise_for_status()
        ids = [m["id"] for m in resp.json().get("data", [])]
    except Exception:
        return None
    for prefix in prefixes:
        for model_id in ids:
            if model_id.startswith(prefix) and (exclude is None or exclude not in model_id):
                return model_id
    return None
