"""SweepConfig parse + cartesian cell expansion."""

from app.llm.registry import ModelSpec
from app.llm.types import ModelRole
from tests.eval.sweep.config import SweepConfig, expand, load_sweep_config


def test_expand_single_role_yields_one_cell_per_candidate() -> None:
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
    cells = expand(config)
    assert len(cells) == 2
    assert cells[0].role_overrides[ModelRole.SMART] == ModelSpec("openrouter", "claude-opus-4-8")
    assert cells[1].role_overrides[ModelRole.SMART] == ModelSpec("ollama", "oss")
    assert all(c.suites == ("rubric",) for c in cells)
    assert {c.id for c in cells} == {"s-000", "s-001"}


def test_expand_is_cartesian_over_roles_and_toggles() -> None:
    config = SweepConfig.model_validate(
        {
            "name": "s",
            "suites": ["rubric"],
            "axes": {"smart": [{"provider": "p", "model": "a"}, {"provider": "p", "model": "b"}]},
            "toggles": {"hybrid": [True, False]},
        }
    )
    cells = expand(config)
    assert len(cells) == 4  # 2 models x 2 toggle values
    assert {(c.role_overrides[ModelRole.SMART].model, c.toggles["hybrid"]) for c in cells} == {
        ("a", True),
        ("a", False),
        ("b", True),
        ("b", False),
    }


def test_expand_empty_axes_yields_one_cell() -> None:
    config = SweepConfig.model_validate({"name": "s", "suites": ["rubric"]})
    cells = expand(config)
    assert len(cells) == 1
    assert cells[0].role_overrides == {}
    assert cells[0].suites == ("rubric",)


def test_load_sweep_config_from_yaml(tmp_path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text("name: s\nsuites: [rubric]\naxes:\n  smart:\n    - {provider: p, model: a}\n")
    config = load_sweep_config(path)
    assert config.name == "s"
    assert config.suites == ["rubric"]
    assert config.axes[ModelRole.SMART][0].model == "a"
