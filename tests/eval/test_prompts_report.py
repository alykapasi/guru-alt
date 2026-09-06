"""The prompt-report delta formatter is pure + correct (Phase 9c). Compile/report runs are manual."""

from tests.eval.prompts.report import format_delta


def test_format_delta_reports_signed_change() -> None:
    out = format_delta(baseline=0.60, candidate=0.80)
    assert "baseline" in out and "compiled" in out
    assert "+0.200" in out  # signed improvement


def test_cli_modules_import_cleanly() -> None:
    import tests.eval.prompts.compile
    import tests.eval.prompts.report  # noqa: F401
