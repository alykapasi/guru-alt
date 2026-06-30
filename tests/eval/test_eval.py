"""Eval gate: the deterministic suites must stay at 100%; rubric runs against a live model.

These are the "first eval cases pass" of the Phase 3 DoD. Grading + tracer are offline and
deterministic, so they gate in CI. The rubric suite needs a real model, so it's skipped
when no local Ollama chat model is available (same pattern as test_ollama_integration).
"""

import harness  # sibling module in tests/eval/ (pytest prepend mode puts it on sys.path)
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ModelRole
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.registry import ModelSpec, fake_llm_client

_OLLAMA = get_settings().ollama_base_url
_CHAT_PREFIXES = ("phi3", "phi4", "llama3", "qwen", "mistral", "gemma3", "granite", "deepseek")


def _pick_chat_model() -> str | None:
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


def _ollama_client(model: str) -> LLMClient:
    """An LLM client with every role (incl. the SMART grader) routed to a local model."""
    provider = OpenAICompatProvider(name="ollama", base_url=_OLLAMA, api_key="")
    return LLMClient({"ollama": provider}, {r: ModelSpec("ollama", model) for r in ModelRole})


def test_grading_eval_gate() -> None:
    report = harness.score_grading(harness.load_grading_cases())
    assert report.total >= 8
    assert report.pass_rate == 1.0, [r.detail for r in report.results if not r.passed]
    assert report.mae is not None and report.mae < 0.01  # deterministic ⇒ essentially exact


def test_tracer_eval_gate() -> None:
    report = harness.score_tracer(harness.load_tracer_cases())
    assert report.total >= 5
    assert report.pass_rate == 1.0, [r.detail for r in report.results if not r.passed]


async def test_retrieval_eval_gate(db_session: AsyncSession) -> None:
    # Recall@k over self-contained golden corpora. Deterministic via the lexical path + RRF,
    # so it gates in CI (DB-backed, no live model).
    report = await harness.score_retrieval(
        db_session, fake_llm_client(), harness.load_retrieval_cases()
    )
    assert report.total >= 4
    assert report.pass_rate == 1.0, [r.detail for r in report.results if not r.passed]


async def test_grounding_eval_gate(db_session: AsyncSession) -> None:
    # The citation contract: a generated block cites only real retrieved chunks, dropping
    # out-of-range/duplicate indices. Scripted model ⇒ deterministic, CI-gated.
    report = await harness.score_grounding(db_session, harness.load_grounding_cases())
    assert report.total >= 4
    assert report.pass_rate == 1.0, [r.detail for r in report.results if not r.passed]


@pytest.mark.skipif(_MODEL is None, reason="Ollama not running or no model pulled")
async def test_rubric_eval_runs_against_live_model() -> None:
    assert _MODEL is not None  # narrow for the type checker; skipif guarantees it
    report = await harness.score_rubric(_ollama_client(_MODEL), harness.load_rubric_cases())
    # Seed level: prove the rubric-grading pipeline runs end-to-end against a real model and
    # yields one result per case (robust to a weak model's malformed output). Accuracy
    # gating waits for a calibrated SMART model + human-labeled set (Phase 8 / DSPy) — a
    # small local model is too noisy to gate on.
    assert report.total == 3
    assert all(r.detail for r in report.results)
