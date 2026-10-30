"""A replay never pays by accident (S59)."""

import pytest

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from tests.eval.labels.replay import PaidProviderRefused, require_free


def _client(**providers: str) -> LLMClient:
    roles = {role: ModelSpec(providers.get(role.name, "ollama"), "m") for role in ModelRole}
    fake = FakeProvider()
    return LLMClient({name: fake for name in {"ollama", "shaped", "openrouter"}}, roles)


def test_local_models_are_free() -> None:
    require_free(_client(), [ModelRole.FAST, ModelRole.SMART], allow_paid=False)


def test_a_paid_role_is_refused() -> None:
    with pytest.raises(PaidProviderRefused, match="openrouter"):
        require_free(_client(SMART="openrouter"), [ModelRole.SMART], allow_paid=False)


def test_a_paid_role_nobody_uses_is_fine() -> None:
    require_free(_client(GENIUS="openrouter"), [ModelRole.FAST], allow_paid=False)


def test_allow_paid_skips_the_guard() -> None:
    require_free(_client(SMART="openrouter"), [ModelRole.SMART], allow_paid=True)
