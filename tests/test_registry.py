"""Registry helpers used by the sweep runner (fresh client per cell, no global mutation)."""

from app.llm.registry import ModelSpec, fake_llm_client
from app.llm.types import ModelRole


def test_with_roles_overrides_only_named_roles() -> None:
    base = fake_llm_client()
    derived = base.with_roles({ModelRole.SMART: ModelSpec(provider="fake", model="smart-2")})

    # the overridden role changes
    assert derived.spec(ModelRole.SMART) == ModelSpec(provider="fake", model="smart-2")
    # an un-overridden role falls through to the base mapping
    assert derived.spec(ModelRole.FAST) == base.spec(ModelRole.FAST)


def test_with_roles_does_not_mutate_the_original() -> None:
    base = fake_llm_client()
    original_smart = base.spec(ModelRole.SMART)
    base.with_roles({ModelRole.SMART: ModelSpec(provider="fake", model="smart-2")})
    assert base.spec(ModelRole.SMART) == original_smart
