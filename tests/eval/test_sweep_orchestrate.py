"""The whole-sweep loop: one run per cell, and a failing cell doesn't abort the matrix."""

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from tests.eval.sweep.config import SweepConfig
from tests.eval.sweep.orchestrate import run_sweep
from tests.eval.sweep.tracking import FakeTracker


def _fake_base(reply: str) -> LLMClient:
    # Providers are stateless connection holders (see CostTrackingClient docstring), so the same
    # fake instance is registered under every provider name the sweep's candidates reference —
    # routing is by name, pricing is by model string, and this offline test needs no real network.
    provider = FakeProvider(reply=reply)
    return LLMClient(
        {"fake": provider, "openrouter": provider, "ollama": provider},
        {r: ModelSpec("fake", "fake-1") for r in ModelRole},
    )


async def test_run_sweep_logs_one_ok_run_per_cell(tmp_path) -> None:
    config = SweepConfig.model_validate(
        {
            "name": "s",
            "suites": ["rubric"],
            "axes": {
                "smart": [
                    {"provider": "openrouter", "model": "claude-opus-4-8"},
                    {"provider": "ollama", "model": "oss"},
                ]
            },
        }
    )
    tracker = FakeTracker()
    await run_sweep(
        config, _fake_base('{"score": 1.0, "rationale": "ok"}'), tracker, artifact_dir=tmp_path
    )

    runs = tracker.list_runs()
    assert len(runs) == 2
    assert all(r.params["status"] == "ok" for r in runs)
    assert all("rubric_pass_rate" in r.metrics and "cost_usd" in r.metrics for r in runs)
    assert runs[0].params["smart_model"] == "openrouter:claude-opus-4-8"
    assert all(len(tracker.artifacts[r.name]) == 1 for r in runs)  # one artifact per suite per cell


async def test_run_sweep_survives_a_failing_cell(tmp_path) -> None:
    # An unknown suite makes the cell raise; the sweep logs it failed and continues.
    config = SweepConfig.model_validate(
        {"name": "s", "suites": ["bogus"], "axes": {"smart": [{"provider": "p", "model": "m"}]}}
    )
    tracker = FakeTracker()
    await run_sweep(config, _fake_base("x"), tracker, artifact_dir=tmp_path)

    runs = tracker.list_runs()
    assert len(runs) == 1
    assert runs[0].params["status"] == "failed"
    assert "rubric_pass_rate" not in runs[0].metrics


async def test_a_cell_naming_an_unknown_provider_fails_alone(tmp_path) -> None:
    """Resolving a cell's models can fail too, and it must not take the rest of the matrix down."""
    config = SweepConfig.model_validate(
        {
            "name": "s",
            "suites": ["rubric"],
            "axes": {
                "smart": [
                    {"provider": "typo", "model": "m"},
                    {"provider": "openrouter", "model": "claude-opus-4-8"},
                ]
            },
        }
    )
    tracker = FakeTracker()
    await run_sweep(
        config, _fake_base('{"score": 1.0, "rationale": "ok"}'), tracker, artifact_dir=tmp_path
    )

    runs = tracker.list_runs()
    assert [r.params["status"] for r in runs] == ["failed", "ok"]
    assert "typo" in runs[0].params["error"]
    assert all(r.params["suites"] == "rubric" for r in runs)  # the failed cell is still identified


async def test_a_run_records_every_role_and_the_dataset_it_scored(tmp_path) -> None:
    """A cell that swept SMART said nothing about the rest of the map, so its own record could
    not reproduce it; and a run's numbers mean nothing without the golden set behind them."""
    config = SweepConfig.model_validate(
        {
            "name": "s",
            "suites": ["kc_tagging"],
            "axes": {"smart": [{"provider": "fake", "model": "fake-1"}]},
            "toggles": {"strict_kc_tagging": [True]},
        }
    )
    tracker = FakeTracker()
    await run_sweep(config, _fake_base("[]"), tracker, artifact_dir=tmp_path)

    params = tracker.list_runs()[0].params
    assert {f"{role.value}_model" for role in ModelRole} <= set(params)
    assert params["applied_kc_tag_min_confidence"] == "0.7"  # the resolution, not the request
    assert len(params["dataset_kc_tagging"]) == 12


async def test_run_sweep_multi_suite_logs_per_suite_metrics_and_artifacts(tmp_path) -> None:
    config = SweepConfig.model_validate(
        {
            "name": "s",
            "suites": ["rubric", "kc_tagging"],
            "axes": {"smart": [{"provider": "fake", "model": "fake-1"}]},
        }
    )
    tracker = FakeTracker()
    await run_sweep(config, _fake_base('{"score": 1.0}'), tracker, artifact_dir=tmp_path)

    runs = tracker.list_runs()
    assert len(runs) == 1
    assert runs[0].params["status"] == "ok"
    assert "rubric_pass_rate" in runs[0].metrics
    assert "kc_tagging_pass_rate" in runs[0].metrics
    assert len(tracker.artifacts[runs[0].name]) == 2  # one artifact per suite
