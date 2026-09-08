"""SweepConfig parse + cartesian cell expansion."""

import pytest

from app.llm.registry import ModelSpec
from app.llm.types import ModelRole
from tests.eval.sweep.config import SweepConfig, expand, load_sweep_config
from tests.eval.sweep.settings import UnsupportedSetting, kc_tag_min_confidence


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
            "toggles": {"strict_kc_tagging": [True, False]},
        }
    )
    cells = expand(config)
    assert len(cells) == 4  # 2 models x 2 toggle values
    assert {
        (c.role_overrides[ModelRole.SMART].model, c.toggles["strict_kc_tagging"]) for c in cells
    } == {
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


# --- a sweep may only vary what it can actually apply ------------------------


def _config(**extra) -> SweepConfig:
    return SweepConfig.model_validate({"name": "s", "suites": ["kc_tagging"], **extra})


def test_a_setting_no_suite_applies_is_refused_before_any_paid_call() -> None:
    """The failure this prevents is invisible afterwards: two identical runs, reported as a
    comparison of the setting between them."""
    with pytest.raises(UnsupportedSetting) as caught:
        expand(_config(gen_config=[{"temperature": 0.2}, {"temperature": 0.9}]))
    assert "temperature" in str(caught.value)
    assert "kc_tag_min_confidence" in str(caught.value)  # says what it could have asked for


def test_an_unapplied_toggle_is_refused_too() -> None:
    with pytest.raises(UnsupportedSetting):
        expand(_config(toggles={"hybrid": [True, False]}))


def test_a_supported_setting_with_an_impossible_value_is_refused() -> None:
    with pytest.raises(UnsupportedSetting) as caught:
        expand(_config(gen_config=[{"kc_tag_min_confidence": 1.5}]))
    assert "between 0.0 and 1.0" in str(caught.value)


def test_a_supported_setting_expands_normally() -> None:
    cells = expand(_config(gen_config=[{"kc_tag_min_confidence": 0.4}, {}]))
    assert [c.gen_config for c in cells] == [{"kc_tag_min_confidence": 0.4}, {}]


def test_an_explicit_confidence_wins_over_the_toggle_shorthand() -> None:
    """Setting both is a contradiction; the number the config wrote is the less surprising win."""
    assert (
        kc_tag_min_confidence({"kc_tag_min_confidence": 0.9}, {"strict_kc_tagging": False}) == 0.9
    )
    assert kc_tag_min_confidence({}, {"strict_kc_tagging": True}) == 0.7
    assert kc_tag_min_confidence({}, {}) == 0.5
