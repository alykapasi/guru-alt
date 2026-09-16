"""Registry helpers used by the sweep runner (fresh client per cell, no global mutation)."""

from app.llm.registry import ModelSpec, fake_llm_client
from app.llm.types import ChatMessage, ChatRole, ModelRole


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


async def test_a_role_can_be_routed_to_the_deterministic_provider_by_name() -> None:
    """What lets a browser journey drive the whole stack with no model behind it (S58).

    Through `build_llm_client`, not a test-only helper: the point is that the journey exercises
    the same routing production uses, with one provider swapped, rather than a second wiring
    that could drift from it.
    """
    from app.core.config import get_settings
    from app.llm.registry import build_llm_client

    settings = get_settings().model_copy(
        update={role: "fake:fake-1" for role in ("model_fast", "model_smart", "model_embed")}
    )
    client = build_llm_client(settings)

    assert client.spec(ModelRole.SMART) == ModelSpec(provider="fake", model="fake-1")
    reply = await client.complete(ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content="hi")])
    assert reply.content
