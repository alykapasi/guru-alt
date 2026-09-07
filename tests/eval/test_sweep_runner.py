"""Cell runner over a scripted FakeProvider — no live model, no DB (for rubric)."""

import pytest

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from tests.eval.sweep.config import Cell
from tests.eval.sweep.runner import run_cell


def _fake_base(reply: str) -> LLMClient:
    return LLMClient(
        {"fake": FakeProvider(reply=reply)}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}
    )


async def test_run_cell_rubric_returns_report_and_cost() -> None:
    # A scripted grade the rubric scorer can parse — proves the suite runs end-to-end.
    base = _fake_base('{"score": 1.0, "rationale": "ok"}')
    cell = Cell(
        id="t-000",
        role_overrides={ModelRole.SMART: ModelSpec("fake", "fake-1")},
        gen_config={},
        toggles={},
        suites=("rubric",),
    )
    reports, cost = await run_cell(cell, base)

    assert len(reports) == 1
    assert reports[0].suite == "rubric"
    assert reports[0].total == 3  # rubric.json has 3 golden cases
    assert cost.total_tokens > 0  # the grader made (fake) completions, tracked


async def test_run_cell_retrieval_requires_a_session() -> None:
    base = _fake_base("x")
    cell = Cell(id="t", role_overrides={}, gen_config={}, toggles={}, suites=("retrieval",))
    with pytest.raises(ValueError, match="requires a database session"):
        await run_cell(cell, base)


async def test_run_cell_rejects_unknown_suite() -> None:
    base = _fake_base("x")
    cell = Cell(id="t", role_overrides={}, gen_config={}, toggles={}, suites=("bogus",))
    with pytest.raises(ValueError, match="unknown or non-sweepable suite"):
        await run_cell(cell, base)


async def test_strict_kc_tagging_toggle_raises_confidence_gate(monkeypatch) -> None:
    # The one concrete toggle wired end-to-end: `strict_kc_tagging` flips the kc_tagging
    # scorer's existing `min_confidence` seam. Spy on the scorer to assert the wiring.
    from tests.eval import harness
    from tests.eval.sweep import runner

    captured: dict[str, float] = {}

    async def spy(client, cases, *, min_confidence: float = 0.5):
        captured["min_confidence"] = min_confidence
        return harness.EvalReport(suite="kc_tagging", results=[])

    monkeypatch.setattr(runner.harness, "score_kc_tagging", spy)
    base = _fake_base("x")

    def cell(strict: bool) -> Cell:
        toggles = {"strict_kc_tagging": True} if strict else {}
        return Cell(
            id="t", role_overrides={}, gen_config={}, toggles=toggles, suites=("kc_tagging",)
        )

    await run_cell(cell(strict=True), base)
    assert captured["min_confidence"] == 0.7
    await run_cell(cell(strict=False), base)
    assert captured["min_confidence"] == 0.5


async def test_a_swept_gen_config_value_reaches_the_call(monkeypatch) -> None:
    """The property the whole sweep rests on: a setting a run logs is a setting a run used."""
    from tests.eval import harness
    from tests.eval.sweep import runner

    captured: list[float] = []

    async def spy(client, cases, *, min_confidence: float = 0.5):
        captured.append(min_confidence)
        return harness.EvalReport(suite="kc_tagging", results=[])

    monkeypatch.setattr(runner.harness, "score_kc_tagging", spy)
    base = _fake_base("x")

    for value in (0.25, 0.85):
        await run_cell(
            Cell(
                id="t",
                role_overrides={},
                gen_config={"kc_tag_min_confidence": value},
                toggles={},
                suites=("kc_tagging",),
            ),
            base,
        )

    assert captured == [0.25, 0.85]
