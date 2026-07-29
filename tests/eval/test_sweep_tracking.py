"""FakeTracker capture + a light MLflowTracker file-store round-trip."""

from tests.eval.sweep.tracking import FakeTracker, MLflowTracker


def test_fake_tracker_records_run_lifecycle() -> None:
    t = FakeTracker()
    t.start_run("cell-a")
    t.log_params({"smart_model": "openrouter:claude-opus-4-8"})
    t.log_metrics({"rubric_pass_rate": 0.9})
    t.log_artifact("/tmp/report.json")
    t.end_run()

    runs = t.list_runs()
    assert len(runs) == 1
    assert runs[0].name == "cell-a"
    assert runs[0].params == {"smart_model": "openrouter:claude-opus-4-8"}
    assert runs[0].metrics == {"rubric_pass_rate": 0.9}
    assert t.artifacts["cell-a"] == ["/tmp/report.json"]


def test_mlflow_tracker_logs_and_reads_back(tmp_path) -> None:
    tracker = MLflowTracker("test-exp", tracking_uri=f"file:{tmp_path / 'mlruns'}")
    tracker.start_run("cell-a")
    tracker.log_params({"smart_model": "claude-opus-4-8"})
    tracker.log_metrics({"rubric_pass_rate": 1.0, "cost_usd": 0.5})
    tracker.end_run()

    runs = tracker.list_runs()
    assert len(runs) == 1
    assert runs[0].name == "cell-a"
    assert runs[0].params["smart_model"] == "claude-opus-4-8"
    assert runs[0].metrics["rubric_pass_rate"] == 1.0
    assert runs[0].metrics["cost_usd"] == 0.5
