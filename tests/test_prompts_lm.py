"""RoleLM routes DSPy calls through our role-based LLMClient (Phase 9c)."""

import asyncio

import dspy

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.prompts.lm import RoleLM


def test_forward_returns_openai_shaped_reply_and_records_usage() -> None:
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply="hello world"))
    resp = lm.forward(messages=[{"role": "user", "content": "hi"}])
    assert resp.choices[0].message.content == "hello world"
    assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens
    assert lm.usage_sum.total_tokens > 0  # usage accumulated for cost logging


async def test_aforward_matches_forward() -> None:
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply="async reply"))
    resp = await lm.aforward(messages=[{"role": "user", "content": "hi"}])
    assert resp.choices[0].message.content == "async reply"


def test_forward_reuses_one_event_loop_across_calls() -> None:
    # C1 regression: forward() must drive every call on ONE persistent loop. A fresh asyncio.run()
    # per call would close the loop the provider's httpx pool is bound to, breaking the 2nd call.
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply="hello"))
    lm.forward(messages=[{"role": "user", "content": "hi"}])
    first_loop = lm._loop
    lm.forward(messages=[{"role": "user", "content": "hi"}])
    assert first_loop is not None
    assert isinstance(first_loop, asyncio.AbstractEventLoop)
    assert lm._loop is first_loop  # same persistent loop reused, not recreated per call


def test_predict_runs_through_rolelm() -> None:
    # End-to-end: a DSPy Predict driven by RoleLM over a fake client that returns the
    # Task-1-confirmed adapter reply format for a single `answer` field.
    lm = RoleLM(
        ModelRole.FAST, fake_llm_client(reply="[[ ## answer ## ]]\n42\n\n[[ ## completed ## ]]")
    )
    with dspy.context(lm=lm):
        pred = dspy.Predict("question -> answer")(question="6x7?")
    assert pred.answer.strip() == "42"
