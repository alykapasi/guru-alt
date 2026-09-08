"""Cost-instrumented LLM client: accumulates token Usage per (role, model), prices it."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.llm import LLMClient
from app.llm.pricing import price_usd
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef, Usage


class RoleCost(BaseModel):
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None = no known price for this model


class CostSummary(BaseModel):
    """``cost_usd`` is None when any role in the cell ran on a model we cannot price.

    A sweep ranks cheapest-first, so pricing an unknown model at 0.0 would have made it the
    recommended configuration on the strength of a number nobody measured.
    """

    cost_usd: float | None
    total_tokens: int
    per_role: list[RoleCost]


class CostTrackingClient(LLMClient):
    """Wraps a client, recording ``Usage`` on every completion, keyed by ``(role, model)``.

    ``stream`` and ``embed`` are delegated **untracked**: the swept chat suites use ``complete``,
    and ``embed`` returns no ``Usage`` to price. Embedding/streaming cost is out of scope for the
    model-selection metric (which sweeps chat roles).

    Build the cell client as ``CostTrackingClient(base.with_roles(...))`` — wrap *after*
    ``with_roles``; the inherited ``with_roles`` returns a plain ``LLMClient`` and would drop tracking.
    """

    def __init__(self, inner: LLMClient) -> None:
        super().__init__(inner._providers, inner._roles)
        self._usage: dict[tuple[ModelRole, str], Usage] = {}

    async def complete(
        self,
        role: ModelRole,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        resp = await super().complete(
            role, messages, system=system, max_tokens=max_tokens, tools=tools
        )
        self._record(role, resp.model, resp.usage)
        return resp

    def _record(self, role: ModelRole, model: str, usage: Usage) -> None:
        key = (role, model)
        prev = self._usage.get(key, Usage())
        self._usage[key] = Usage(
            input_tokens=prev.input_tokens + usage.input_tokens,
            output_tokens=prev.output_tokens + usage.output_tokens,
        )

    def cost_summary(self) -> CostSummary:
        per_role = [
            RoleCost(
                role=role.value,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=price_usd(self.spec(role).provider, model, usage),
            )
            for (role, model), usage in sorted(self._usage.items())
        ]
        priced = [rc.cost_usd for rc in per_role]
        return CostSummary(
            cost_usd=None if any(c is None for c in priced) else sum(priced),
            total_tokens=sum(rc.input_tokens + rc.output_tokens for rc in per_role),
            per_role=per_role,
        )
