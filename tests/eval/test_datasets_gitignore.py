"""Governance (D3): mined datasets must never be committable."""

import subprocess


def test_mined_datasets_are_gitignored() -> None:
    target = "tests/eval/datasets/tracer-calibration.json"
    result = subprocess.run(["git", "check-ignore", target], capture_output=True, text=True)
    assert result.returncode == 0, f"{target} is NOT gitignored (governance D3)"
    assert target in result.stdout
