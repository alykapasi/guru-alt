"""Every non-local model named in a shipped sweep config must be priced (no silent $0)."""

from pathlib import Path

from app.llm.pricing import _PRICES
from tests.eval.sweep.config import load_sweep_config

EXPERIMENTS = Path(__file__).parent / "experiments"


def test_shipped_configs_price_every_paid_model() -> None:
    configs = sorted(EXPERIMENTS.glob("*.yaml"))
    assert configs, "no shipped sweep configs found"
    for config_path in configs:
        config = load_sweep_config(config_path)
        for role, candidates in config.axes.items():
            for candidate in candidates:
                if candidate.provider == "ollama":
                    continue  # local models are intentionally free ($0)
                assert any(key in candidate.model for key in _PRICES), (
                    f"{config_path.name}: {role.value} candidate {candidate.model!r} "
                    f"has no entry in app.llm.pricing._PRICES"
                )
