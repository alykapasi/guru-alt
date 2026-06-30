"""Model-role registry + the client services talk to.

Code calls ``client.stream(ModelRole.SMART, messages)``; the registry resolves the role
to a `(provider, model)` from settings and dispatches. Swapping a model is a config edit.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.providers import AnthropicProvider, FakeProvider, OpenAICompatProvider
from app.llm.types import ChatChunk, ChatMessage, ChatResponse, ModelRole


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str


def _parse_spec(value: str) -> ModelSpec:
    """Parse a ``"provider:model"`` config value."""
    provider, _, model = value.partition(":")
    if not provider or not model:
        raise ValueError(f"invalid model spec {value!r}; expected 'provider:model'")
    return ModelSpec(provider=provider, model=model)


class LLMClient:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        roles: dict[ModelRole, ModelSpec],
    ) -> None:
        self._providers = providers
        self._roles = roles

    def spec(self, role: ModelRole) -> ModelSpec:
        return self._roles[role]

    def _resolve(self, role: ModelRole) -> tuple[LLMProvider, str]:
        spec = self._roles[role]
        return self._providers[spec.provider], spec.model

    async def complete(
        self,
        role: ModelRole,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        provider, model = self._resolve(role)
        return await provider.complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens
        )

    def stream(
        self,
        role: ModelRole,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[ChatChunk]:
        provider, model = self._resolve(role)
        return provider.stream(model=model, messages=messages, system=system, max_tokens=max_tokens)

    async def embed(self, role: ModelRole, texts: Sequence[str]) -> list[list[float]]:
        provider, model = self._resolve(role)
        return await provider.embed(model=model, texts=texts)


def build_llm_client(settings: Settings) -> LLMClient:
    """Construct the registry from settings (one provider instance per backend)."""
    providers: dict[str, LLMProvider] = {
        "ollama": OpenAICompatProvider(
            name="ollama", base_url=settings.ollama_base_url, api_key=""
        ),
        "openrouter": OpenAICompatProvider(
            name="openrouter",
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
        ),
        "anthropic": AnthropicProvider(api_key=settings.anthropic_api_key),
    }
    roles = {
        ModelRole.FAST: _parse_spec(settings.model_fast),
        ModelRole.SMART: _parse_spec(settings.model_smart),
        ModelRole.GENIUS: _parse_spec(settings.model_genius),
        ModelRole.EMBED: _parse_spec(settings.model_embed),
    }
    return LLMClient(providers, roles)


def fake_llm_client(reply: str = "Hello from the fake tutor.") -> LLMClient:
    """A registry where every role is the deterministic FakeProvider (for tests)."""
    fake = FakeProvider(reply=reply)
    providers: dict[str, LLMProvider] = {"fake": fake}
    roles = {role: ModelSpec(provider="fake", model="fake-1") for role in ModelRole}
    return LLMClient(providers, roles)
