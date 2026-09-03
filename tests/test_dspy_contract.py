"""Pins the DSPy custom-LM legacy contract + adapter reply shape this codebase relies on.

If a dspy upgrade breaks this, RoleLM (app/prompts/lm.py) and the kc_tagging program must be
revisited. Runs fully offline — the fake LM returns canned text, no network.
"""

import types

import dspy


class _FakeLM(dspy.BaseLM):
    forward_contract = "legacy"

    def __init__(self, reply: str) -> None:
        super().__init__(model="fake:test")
        self._reply = reply

    def forward(self, prompt=None, messages=None, **kwargs):
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=self._reply))],
            usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            model="fake:test",
        )

    async def aforward(self, prompt=None, messages=None, **kwargs):
        return self.forward(prompt=prompt, messages=messages, **kwargs)


def test_legacy_baselm_predict_parses_output() -> None:
    # A single-output signature; the reply is the adapter's expected field format for `answer`.
    lm = _FakeLM(reply="[[ ## answer ## ]]\nblue\n\n[[ ## completed ## ]]")
    with dspy.context(lm=lm):
        pred = dspy.Predict("question -> answer")(question="What color is the sky?")
    assert pred.answer.strip() == "blue"


async def test_legacy_baselm_async_acall_parses_output() -> None:
    lm = _FakeLM(reply="[[ ## answer ## ]]\ngreen\n\n[[ ## completed ## ]]")
    with dspy.context(lm=lm):
        pred = await dspy.Predict("question -> answer").acall(question="What color is grass?")
    assert pred.answer.strip() == "green"
