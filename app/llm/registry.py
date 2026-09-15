"""Model-role registry + the client services talk to.

Code calls ``client.stream(ModelRole.SMART, messages)``; the registry resolves the role
to a `(provider, model)` from settings and dispatches. Swapping a model is a config edit.
"""

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.providers import (
    AnthropicProvider,
    FakeProvider,
    FakeTurn,
    OpenAICompatProvider,
    ShapedProvider,
)
from app.llm.types import ChatChunk, ChatMessage, ChatResponse, EmbedResult, ModelRole, ToolDef


class LLMConfigError(ValueError):
    """The role→model map cannot be served by the configured providers.

    Raised while *building* the registry, so a typo in ``GURU_MODEL_*`` stops the process at
    startup instead of surfacing as a ``KeyError`` inside a learner's turn.
    """


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


def _validate(providers: dict[str, LLMProvider], roles: dict[ModelRole, ModelSpec]) -> None:
    """Every role must name a provider that exists and can do the job that role implies."""
    known = sorted(providers)
    for role, spec in roles.items():
        setting = f"GURU_MODEL_{role.value.upper()}"
        provider = providers.get(spec.provider)
        if provider is None:
            raise LLMConfigError(
                f"{setting}={spec.provider}:{spec.model} names an unknown provider "
                f"{spec.provider!r}; known providers are {', '.join(known)}"
            )
        if role is ModelRole.EMBED and not provider.supports_embeddings:
            usable = [p for p in known if providers[p].supports_embeddings]
            raise LLMConfigError(
                f"{setting} routes to {spec.provider!r}, which has no embeddings API; "
                f"use one of {', '.join(usable)}"
            )


def _timed[T: (ChatResponse, EmbedResult)](result: T, started: float) -> T:
    """Attach the elapsed provider time to the usage travelling with ``result``.

    Measured here rather than at each call site: this is the one place every non-streaming
    provider call passes through, so timing it means no service has to remember to, and none
    can measure a different span from the others. Streaming is deliberately not timed — there
    is no single end to a stream, and time-to-first-token and time-to-completion are different
    questions that one number would blur (S48, S51).
    """
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result.model_copy(
        update={"usage": result.usage.model_copy(update={"latency_ms": elapsed_ms})}
    )


class LLMClient:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        roles: dict[ModelRole, ModelSpec],
    ) -> None:
        _validate(providers, roles)
        self._providers = providers
        self._roles = roles

    def spec(self, role: ModelRole) -> ModelSpec:
        return self._roles[role]

    def with_roles(self, overrides: dict[ModelRole, "ModelSpec"]) -> "LLMClient":
        """A new client sharing this client's providers, with ``overrides`` merged over its roles.

        The sweep builds one fresh client per cell (D9); this never mutates ``self``. Providers are
        stateless connection holders, so sharing them across the two clients is safe.
        """
        return LLMClient(self._providers, {**self._roles, **overrides})

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
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        provider, model = self._resolve(role)
        started = time.perf_counter()
        response = await provider.complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        )
        return _timed(response, started)

    def stream(
        self,
        role: ModelRole,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        provider, model = self._resolve(role)
        return provider.stream(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        )

    async def embed(self, role: ModelRole, texts: Sequence[str]) -> EmbedResult:
        provider, model = self._resolve(role)
        started = time.perf_counter()
        result = await provider.embed(model=model, texts=texts)
        return _timed(result, started)


def build_llm_client(settings: Settings) -> LLMClient:
    """Construct the registry from settings (one provider instance per backend).

    Raises :class:`LLMConfigError` if any role names a provider that does not exist or cannot
    serve that role. Called from the app's lifespan so a bad map fails at startup.
    """
    limits = {"timeout": settings.llm_timeout_seconds, "max_retries": settings.llm_max_retries}
    providers: dict[str, LLMProvider] = {
        "ollama": OpenAICompatProvider(
            name="ollama", base_url=settings.ollama_base_url, api_key="", **limits
        ),
        "openrouter": OpenAICompatProvider(
            name="openrouter",
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            **limits,
        ),
        "anthropic": AnthropicProvider(api_key=settings.anthropic_api_key, **limits),
        # Reachable by naming it in a role map (`GURU_MODEL_SMART=fake:fake-1`), which is what
        # lets a browser journey drive the whole stack without a model. It is registered here
        # rather than behind a separate switch so it goes through the same routing every other
        # provider does — and `app.core.release` refuses to start production with any role
        # pointing at it, on the same reasoning as the dev-login seam: a stack answering from a
        # canned reply looks exactly like a stack that is working.
        "fake": FakeProvider(),
        # The same seam, for the callers the plain fake cannot serve: curriculum design, item
        # writing and grading all parse the reply as JSON, so one canned sentence sends every
        # one of them down its parse-failure path. This answers each in its own shape, which is
        # what lets a browser journey reach a generated curriculum or a graded answer at all.
        "shaped": ShapedProvider(),
    }
    roles = {
        ModelRole.FAST: _parse_spec(settings.model_fast),
        ModelRole.SMART: _parse_spec(settings.model_smart),
        ModelRole.GENIUS: _parse_spec(settings.model_genius),
        ModelRole.VISION: _parse_spec(settings.model_vision),
        ModelRole.EMBED: _parse_spec(settings.model_embed),
    }
    return LLMClient(providers, roles)


def fake_llm_client(
    reply: str = "Hello from the fake tutor.", *, script: Sequence[FakeTurn] | None = None
) -> LLMClient:
    """A registry where every role is the deterministic FakeProvider (for tests)."""
    fake = FakeProvider(reply=reply, script=script)
    providers: dict[str, LLMProvider] = {"fake": fake}
    roles = {role: ModelSpec(provider="fake", model="fake-1") for role in ModelRole}
    return LLMClient(providers, roles)
