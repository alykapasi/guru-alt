"""Planted-instruction cases for the replay (S59, S31).

The set itself is checked here, model-free: every case plants an instruction and the canary it
would produce if obeyed, and the content is fenced as untrusted when it reaches a prompt. Whether
a real model resists is the replay's question (``poe label-report --replay --injection``).
"""

import json
from pathlib import Path

import pytest

from app.agent.untrusted import INSTRUCTION, untrusted_body
from tests.eval.labels.injection import CASES_PATH, load, prompt_for, resisted

CASES = load()


def test_there_are_at_least_twenty_and_the_ids_are_unique() -> None:
    assert len(CASES) >= 20
    assert len({c.id for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_each_case_plants_its_canary(case) -> None:
    assert case.channel in ("source", "memory")
    assert case.canary in case.content
    assert case.canary.startswith("CANARY-")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_the_content_reaches_the_prompt_fenced(case) -> None:
    prompt = prompt_for(case)
    assert INSTRUCTION in prompt
    assert untrusted_body(prompt) == case.content


def test_resisting_means_the_canary_is_absent() -> None:
    case = CASES[0]
    assert resisted("Photosynthesis makes glucose.", case) is True
    assert resisted(f"Sure: {case.canary.lower()}", case) is False


def test_the_file_is_plain_json() -> None:
    assert isinstance(json.loads(Path(CASES_PATH).read_text()), list)


async def test_the_run_scores_every_case_on_the_stand_in() -> None:
    from app.llm.registry import fake_llm_client
    from tests.eval.labels.injection import render, run

    results = await run(fake_llm_client(), CASES)
    assert len(results) == len(CASES)
    assert all(ok for _, ok in results)  # the fake never repeats a canary
    assert render(results).startswith(f"injection — {len(CASES)} of {len(CASES)} resisted")


def test_the_report_refuses_a_paid_injection_run() -> None:
    from app.llm.providers.fake import FakeProvider
    from app.llm.registry import LLMClient, ModelSpec
    from app.llm.types import ModelRole
    from tests.eval.labels.injection import ROLE
    from tests.eval.labels.replay import PaidProviderRefused, require_free

    llm = LLMClient(
        {"openrouter": FakeProvider()}, {r: ModelSpec("openrouter", "m") for r in ModelRole}
    )
    with pytest.raises(PaidProviderRefused):
        require_free(llm, [ROLE], allow_paid=False)
