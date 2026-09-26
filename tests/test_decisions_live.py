"""A handful of synthetic turns against the real Jev service (S78). Opt-in and paid.

Skipped unless ``GURU_JEV_SMOKE=1`` — the default suite and CI never reach the network. It
checks the shapes the adapter maps and reports latency; it asserts nothing about Jev's judgment,
which is what the shadow report measures on real traffic.

Run: ``GURU_JEV_SMOKE=1 uv run pytest tests/test_decisions_live.py -v -s``
"""

import os

import pytest

from app.core.config import get_settings
from app.learning.turn_read import FULLY_CORRECT, INTENT, QUESTIONS, build_state
from app.llm.decisions import ChoiceAnswer, DecisionResponse, TypeSafeDecisionClient, YesNoAnswer

pytestmark = pytest.mark.skipif(
    os.environ.get("GURU_JEV_SMOKE", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="live Jev smoke test: set GURU_JEV_SMOKE=1 (paid, needs GURU_TYPESAFE_API_KEY)",
)

TURNS = [
    ("What is velocity?", "Speed in a given direction."),
    ("What is velocity?", "what does velocity even mean?"),
    ("What is velocity?", "can we talk about something else"),
    ("Define inertia.", "Inertia is a kind of fruit."),
]


@pytest.mark.parametrize(("stem", "reply"), TURNS)
async def test_jev_answers_in_the_shapes_the_adapter_expects(stem: str, reply: str) -> None:
    key = get_settings().typesafe_api_key.get_secret_value()
    if not key.strip():
        pytest.skip("GURU_TYPESAFE_API_KEY is empty")
    client = TypeSafeDecisionClient(api_key=key, model=get_settings().decision_model)

    result = await client.read(
        build_state(stem=stem, message=reply, rubric_criteria=None),
        {INTENT: QUESTIONS[INTENT], FULLY_CORRECT: QUESTIONS[FULLY_CORRECT]},
        timeout_s=10.0,
    )

    assert isinstance(result, DecisionResponse), result
    assert isinstance(result.answers[INTENT], ChoiceAnswer)
    assert isinstance(result.answers[FULLY_CORRECT], YesNoAnswer)
    print(f"\n{reply!r}: {result.answers} · {result.latency_ms} ms · {result.input_tokens} tokens")
