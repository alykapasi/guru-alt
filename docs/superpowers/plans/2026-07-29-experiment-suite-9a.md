# Phase 9a — Experiment & Sweep Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command (`poe sweep <config.yaml>`) runs a role→model × config sweep over the existing eval suites, capturing quality **and cost** per configuration into MLflow, so we can pick a defensible prod role→model map for the alpha rollout and answer "does component X earn its cost?"

**Architecture:** An offline, on-demand runner layered on the existing `tests/eval/harness.py`. A declarative YAML sweep config expands into config **cells** (one fully-resolved configuration each); each cell runs its selected model-driven suites through a **cost-instrumented** `LLMClient` built with that cell's role→model overrides; params + metrics + artifacts land in a pluggable **`Tracker`** (MLflow local file store), one cell = one run; a separate report command reads runs back for a leaderboard + pairwise ablation diff. The runner never mutates global state (fresh client per cell) and never enters the CI gate — `poe eval` stays exactly as-is.

**Tech Stack:** Python 3.13, Pydantic v2, pytest (asyncio_mode=auto), PyYAML (config), MLflow (tracking, local file store), the existing `app.llm` registry + `app.llm.pricing`.

**Spec:** `docs/superpowers/specs/2026-07-28-experiment-suite-design.md`.

## Global Constraints

- **Python ≥ 3.13.** ruff line-length 100 (E501 ignored); `uv run poe check` (lint + type-check + test) must end green.
- **All new runner code lives under `tests/eval/sweep/`** — a namespace package (NO `__init__.py`, matching how `python -m tests.eval.harness` already works). Config files live in `tests/eval/experiments/`.
- **Imports use absolute paths** — `from app.llm... import` and `from tests.eval.harness import` / `from tests.eval.sweep.<mod> import`. This resolves both under pytest (repo root is on `sys.path`) and under `python -m tests.eval.sweep` (repo root on `sys.path`). Do NOT use bare `import harness` in sweep code.
- **New non-test modules must NOT match `test_*.py`** (pytest would collect them). Test files are `tests/eval/test_sweep_*.py` and `tests/test_registry.py`.
- **LLM access stays role-based via the registry.** Never call a provider SDK or hardcode a model name in runner code — model names come **only** from the sweep config.
- **Exactly one `app/` runtime touch:** `LLMClient.with_roles(...)` in `app/llm/registry.py` — a pure constructor helper (D9's "fresh client per cell, never mutate the global registry"). No behavior change to any existing caller. No other `app/` change.
- **`grading`/`tracer` (deterministic) and `grounding` (scripted citation-contract) are NOT sweepable.** Sweepable suites: `rubric` (SMART role), `kc_tagging` (FAST role), `retrieval` (EMBED role, needs a DB session).
- **Cost tracking covers `complete` only.** `stream` and `embed` are pass-through, untracked (documented) — the swept chat suites use `complete`; embeddings return no `Usage` to price.
- **`mlflow` + `PyYAML` are dev-group deps only** (not runtime app deps). `mlruns/` is gitignored.
- **Known facts (verified against the code):** `Usage(input_tokens, output_tokens)` with a `.total_tokens` property (NOT `.total`). `cost_usd(model, usage)` matches `_PRICES` keys by substring; unknown/local models → `$0`. `rubric` → `ModelRole.SMART`; `kc_tagging` → `ModelRole.FAST`. `rubric.json` has 3 cases.

## File Structure

**Created (new):**
- `tests/eval/sweep/config.py` — `SweepConfig`, `ModelCandidate`, `Cell`, `load_sweep_config`, `expand`.
- `tests/eval/sweep/cost.py` — `CostTrackingClient`, `CostSummary`, `RoleCost`.
- `tests/eval/sweep/runner.py` — `run_cell` (one cell → reports + cost).
- `tests/eval/sweep/tracking.py` — `Tracker` protocol, `RunRecord`, `FakeTracker`, `MLflowTracker`.
- `tests/eval/sweep/report.py` — `leaderboard`, `pairwise_diff` (pure), plus the `sweep-report` CLI `main()`.
- `tests/eval/sweep/orchestrate.py` — `run_sweep` (expand → run cells → log per-cell runs, survives cell failure).
- `tests/eval/sweep/__main__.py` — the `poe sweep` CLI entry.
- `tests/eval/experiments/role-selection-v1.yaml` — the first experiment config.
- Tests: `tests/test_registry.py`, `tests/eval/test_sweep_config.py`, `tests/eval/test_sweep_cost.py`, `tests/eval/test_sweep_runner.py`, `tests/eval/test_sweep_tracking.py`, `tests/eval/test_sweep_report.py`, `tests/eval/test_sweep_orchestrate.py`, `tests/eval/test_sweep_pricing_table.py`.

**Modified:**
- `app/llm/registry.py` — add `LLMClient.with_roles`.
- `pyproject.toml` — dev deps (`pyyaml`, `mlflow`) via `uv add`; two `[tool.poe.tasks]` entries.
- `.gitignore` — add `mlruns/`.

---

### Task 1: `LLMClient.with_roles` — the fresh-client-per-cell seam (D9)

**Files:**
- Modify: `app/llm/registry.py` (add a method to `LLMClient`, near `spec`/`_resolve` around line 39-44)
- Test: `tests/test_registry.py` (create)

**Interfaces:**
- Consumes: existing `LLMClient.__init__(providers, roles)`, `ModelSpec`, `ModelRole`.
- Produces: `LLMClient.with_roles(self, overrides: dict[ModelRole, ModelSpec]) -> LLMClient` — a new client sharing this client's providers, with `overrides` merged over its role map; the original is unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL — `AttributeError: 'LLMClient' object has no attribute 'with_roles'`

- [ ] **Step 3: Add the method**

In `app/llm/registry.py`, inside `class LLMClient`, add after `spec` (line 39-40):

```python
    def with_roles(self, overrides: dict[ModelRole, "ModelSpec"]) -> "LLMClient":
        """A new client sharing this client's providers, with ``overrides`` merged over its roles.

        The sweep builds one fresh client per cell (D9); this never mutates ``self``. Providers are
        stateless connection holders, so sharing them across the two clients is safe.
        """
        return LLMClient(self._providers, {**self._roles, **overrides})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_registry.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/llm/registry.py tests/test_registry.py
git commit -m "feat(9a): LLMClient.with_roles — fresh client per sweep cell"
```

---

### Task 2: Sweep config + cell expansion

**Files:**
- Create: `tests/eval/sweep/config.py`
- Test: `tests/eval/test_sweep_config.py`
- Modify: `pyproject.toml` (add `pyyaml` dev dep — Step 0)

**Interfaces:**
- Consumes: `ModelSpec` (`app.llm.registry`), `ModelRole` (`app.llm.types`), PyYAML.
- Produces:
  - `ModelCandidate(BaseModel)` with `provider: str`, `model: str`, `.to_spec() -> ModelSpec`.
  - `SweepConfig(BaseModel)` with `name: str`, `suites: list[str]`, `axes: dict[ModelRole, list[ModelCandidate]]`, `gen_config: list[dict]`, `toggles: dict[str, list[bool]]`.
  - `Cell` (frozen dataclass): `id: str`, `role_overrides: dict[ModelRole, ModelSpec]`, `gen_config: dict`, `toggles: dict[str, bool]`, `suites: tuple[str, ...]`.
  - `load_sweep_config(path: str | Path) -> SweepConfig`.
  - `expand(config: SweepConfig) -> list[Cell]` — cartesian product over axes × gen_config × toggles.

- [ ] **Step 0: Add PyYAML as a dev dependency**

Run: `uv add --group dev pyyaml`
Expected: `pyproject.toml` gains `pyyaml` under `[dependency-groups] dev`; `uv.lock` updates. (Import name is `yaml`.)

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_sweep_config.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_sweep_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.sweep'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/sweep/config.py`:

```python
"""Sweep config: a declarative YAML matrix expanded into fully-resolved cells."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.llm.registry import ModelSpec
from app.llm.types import ModelRole


class ModelCandidate(BaseModel):
    """One candidate model for a role axis."""

    provider: str
    model: str

    def to_spec(self) -> ModelSpec:
        return ModelSpec(provider=self.provider, model=self.model)


class SweepConfig(BaseModel):
    """A parsed sweep file. ``axes`` maps a role to its candidate models."""

    name: str
    suites: list[str]
    axes: dict[ModelRole, list[ModelCandidate]] = Field(default_factory=dict)
    gen_config: list[dict] = Field(default_factory=lambda: [{}])
    toggles: dict[str, list[bool]] = Field(default_factory=dict)


@dataclass(frozen=True)
class Cell:
    """One fully-resolved experiment configuration (one point in the sweep matrix)."""

    id: str
    role_overrides: dict[ModelRole, ModelSpec]
    gen_config: dict
    toggles: dict[str, bool]
    suites: tuple[str, ...]


def load_sweep_config(path: str | Path) -> SweepConfig:
    data = yaml.safe_load(Path(path).read_text())
    return SweepConfig.model_validate(data)


def expand(config: SweepConfig) -> list[Cell]:
    """Cartesian product of every axis (roles × gen_config × toggles) → one Cell per combination."""
    roles = list(config.axes)
    role_choices = [config.axes[r] for r in roles]
    toggle_names = list(config.toggles)
    toggle_choices = [config.toggles[t] for t in toggle_names]

    role_combos = itertools.product(*role_choices) if role_choices else [()]
    toggle_combos = itertools.product(*toggle_choices) if toggle_choices else [()]

    cells: list[Cell] = []
    for i, (role_pick, gen, toggle_pick) in enumerate(
        itertools.product(role_combos, config.gen_config or [{}], toggle_combos)
    ):
        cells.append(
            Cell(
                id=f"{config.name}-{i:03d}",
                role_overrides={roles[j]: role_pick[j].to_spec() for j in range(len(roles))},
                gen_config=gen,
                toggles={toggle_names[j]: toggle_pick[j] for j in range(len(toggle_names))},
                suites=tuple(config.suites),
            )
        )
    return cells
```

Note: `itertools.product` consumes its iterables once — `role_combos`/`toggle_combos` are materialized as needed inside the single outer `product`, which is fine because each is iterated exactly once there.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_sweep_config.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/sweep/config.py tests/eval/test_sweep_config.py pyproject.toml uv.lock
git commit -m "feat(9a): sweep config parse + cartesian cell expansion"
```

---

### Task 3: Cost-instrumented client

**Files:**
- Create: `tests/eval/sweep/cost.py`
- Test: `tests/eval/test_sweep_cost.py`

**Interfaces:**
- Consumes: `LLMClient` (subclassed), `cost_usd` (`app.llm.pricing`), `Usage`, `ModelRole`, `ChatMessage`, `ChatResponse`, `ToolDef` (`app.llm.types`).
- Produces:
  - `RoleCost(BaseModel)`: `role: str`, `model: str`, `input_tokens: int`, `output_tokens: int`, `cost_usd: float`.
  - `CostSummary(BaseModel)`: `cost_usd: float`, `total_tokens: int`, `per_role: list[RoleCost]`.
  - `CostTrackingClient(LLMClient)`: `__init__(inner: LLMClient)`; overrides `complete` to record `Usage` per `(role, model)`; `.cost_summary() -> CostSummary`.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_sweep_cost.py`:

```python
"""Cost accumulation + pricing through the cost-instrumented client."""

import pytest

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatRole, ModelRole
from tests.eval.sweep.cost import CostTrackingClient


def _client(reply: str, model: str) -> CostTrackingClient:
    inner = LLMClient({"fake": FakeProvider(reply=reply)}, {r: ModelSpec("fake", model) for r in ModelRole})
    return CostTrackingClient(inner)


async def test_accumulates_usage_per_role_and_model() -> None:
    client = _client("one two three", model="claude-opus-4-8")  # 3 output words
    msg = [ChatMessage(role=ChatRole.USER, content="a b")]  # 2 input words
    await client.complete(ModelRole.SMART, msg)
    await client.complete(ModelRole.SMART, msg)

    summary = client.cost_summary()
    assert len(summary.per_role) == 1
    rc = summary.per_role[0]
    assert rc.role == "smart" and rc.model == "claude-opus-4-8"
    assert rc.input_tokens == 4  # 2 calls x 2 input words
    assert rc.output_tokens == 6  # 2 calls x 3 output words
    # opus price = (5.0, 25.0) per 1M tokens
    assert rc.cost_usd == pytest.approx(4 / 1_000_000 * 5.0 + 6 / 1_000_000 * 25.0)
    assert summary.total_tokens == 10
    assert summary.cost_usd == pytest.approx(rc.cost_usd)


async def test_unknown_model_prices_to_zero() -> None:
    client = _client("hi there", model="some-oss-model")  # not in _PRICES
    await client.complete(ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content="q")])
    summary = client.cost_summary()
    assert summary.cost_usd == 0.0
    assert summary.total_tokens == 3  # 1 input + 2 output words
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_sweep_cost.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.sweep.cost'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/sweep/cost.py`:

```python
"""Cost-instrumented LLM client: accumulates token Usage per (role, model), prices it."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.llm import LLMClient
from app.llm.pricing import cost_usd
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef, Usage


class RoleCost(BaseModel):
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CostSummary(BaseModel):
    cost_usd: float
    total_tokens: int
    per_role: list[RoleCost]


class CostTrackingClient(LLMClient):
    """Wraps a client, recording ``Usage`` on every completion, keyed by ``(role, model)``.

    ``stream`` and ``embed`` are delegated **untracked**: the swept chat suites use ``complete``,
    and ``embed`` returns no ``Usage`` to price. Embedding/streaming cost is out of scope for the
    model-selection metric (which sweeps chat roles).
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
                cost_usd=cost_usd(model, usage),
            )
            for (role, model), usage in sorted(self._usage.items())
        ]
        return CostSummary(
            cost_usd=sum(rc.cost_usd for rc in per_role),
            total_tokens=sum(rc.input_tokens + rc.output_tokens for rc in per_role),
            per_role=per_role,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_sweep_cost.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/sweep/cost.py tests/eval/test_sweep_cost.py
git commit -m "feat(9a): cost-instrumented LLM client (usage per role+model, priced)"
```

---

### Task 4: Cell runner

**Files:**
- Create: `tests/eval/sweep/runner.py`
- Test: `tests/eval/test_sweep_runner.py`

**Interfaces:**
- Consumes: `Cell` (Task 2), `CostTrackingClient`/`CostSummary` (Task 3), `LLMClient.with_roles` (Task 1), the harness scorers (`score_rubric`, `score_kc_tagging`, `score_retrieval`) + loaders + `EvalReport`.
- Produces: `run_cell(cell: Cell, base_client: LLMClient, *, session: AsyncSession | None = None) -> tuple[list[harness.EvalReport], CostSummary]`. Applies the cell's `toggles` per suite (the concrete toggle: `strict_kc_tagging` raises the `kc_tagging` confidence gate).

**Toggle decision (spec §7/§12):** there is **no** existing app seam for a component toggle — `app/rag/retrieval.py::retrieve` has no hybrid/vector switch and `Settings` has no retrieval flag (verified). Spec §7's YAGNI clause forbids inventing a new app seam now. So the one concrete toggle wired end-to-end flips an **existing** seam — the `kc_tagging` scorer's own `min_confidence` parameter — proving the toggle path (config → `Cell.toggles` → applied in the run → logged param → pairwise diff) with zero app-code change. Component-behavior toggles are added when their app seam first lands.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_sweep_runner.py`:

```python
"""Cell runner over a scripted FakeProvider — no live model, no DB (for rubric)."""

import pytest

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from tests.eval.sweep.config import Cell
from tests.eval.sweep.runner import run_cell


def _fake_base(reply: str) -> LLMClient:
    return LLMClient({"fake": FakeProvider(reply=reply)}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def test_run_cell_rubric_returns_report_and_cost() -> None:
    # A scripted grade the rubric scorer can parse — proves the suite runs end-to-end.
    base = _fake_base('{"score": 1.0, "rationale": "ok"}')
    cell = Cell(
        id="t-000",
        role_overrides={ModelRole.SMART: ModelSpec("fake", "fake-1")},
        gen_config={},
        toggles={},
        suites=("rubric",),
    )
    reports, cost = await run_cell(cell, base)

    assert len(reports) == 1
    assert reports[0].suite == "rubric"
    assert reports[0].total == 3  # rubric.json has 3 golden cases
    assert cost.total_tokens > 0  # the grader made (fake) completions, tracked


async def test_run_cell_retrieval_requires_a_session() -> None:
    base = _fake_base("x")
    cell = Cell(id="t", role_overrides={}, gen_config={}, toggles={}, suites=("retrieval",))
    with pytest.raises(ValueError, match="requires a database session"):
        await run_cell(cell, base)


async def test_run_cell_rejects_unknown_suite() -> None:
    base = _fake_base("x")
    cell = Cell(id="t", role_overrides={}, gen_config={}, toggles={}, suites=("bogus",))
    with pytest.raises(ValueError, match="unknown or non-sweepable suite"):
        await run_cell(cell, base)


async def test_strict_kc_tagging_toggle_raises_confidence_gate(monkeypatch) -> None:
    # The one concrete toggle wired end-to-end: `strict_kc_tagging` flips the kc_tagging
    # scorer's existing `min_confidence` seam. Spy on the scorer to assert the wiring.
    from tests.eval import harness
    from tests.eval.sweep import runner

    captured: dict[str, float] = {}

    async def spy(client, cases, *, min_confidence: float = 0.5):
        captured["min_confidence"] = min_confidence
        return harness.EvalReport(suite="kc_tagging", results=[])

    monkeypatch.setattr(runner.harness, "score_kc_tagging", spy)
    base = _fake_base("x")

    def cell(strict: bool) -> Cell:
        toggles = {"strict_kc_tagging": True} if strict else {}
        return Cell(id="t", role_overrides={}, gen_config={}, toggles=toggles, suites=("kc_tagging",))

    await run_cell(cell(strict=True), base)
    assert captured["min_confidence"] == 0.7
    await run_cell(cell(strict=False), base)
    assert captured["min_confidence"] == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_sweep_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.sweep.runner'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/sweep/runner.py`:

```python
"""Run one Cell: build its client, run its suites over the golden cases, price the calls."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from tests.eval import harness
from tests.eval.sweep.config import Cell
from tests.eval.sweep.cost import CostSummary, CostTrackingClient


async def run_cell(
    cell: Cell,
    base_client: LLMClient,
    *,
    session: AsyncSession | None = None,
) -> tuple[list[harness.EvalReport], CostSummary]:
    """Run ``cell``'s suites against a fresh, cost-tracked client; return reports + cost.

    ``base_client`` supplies the providers + ambient role map; the cell's ``role_overrides`` are
    merged over it (never mutating ``base_client``). A ``retrieval`` suite needs ``session``.
    """
    client = CostTrackingClient(base_client.with_roles(cell.role_overrides))
    reports = [await _run_suite(suite, client, session, cell.toggles) for suite in cell.suites]
    return reports, client.cost_summary()


async def _run_suite(
    suite: str,
    client: LLMClient,
    session: AsyncSession | None,
    toggles: dict[str, bool],
) -> harness.EvalReport:
    if suite == "rubric":
        return await harness.score_rubric(client, harness.load_rubric_cases())
    if suite == "kc_tagging":
        # Concrete toggle wired end-to-end: `strict_kc_tagging` raises the confidence gate
        # (flips the scorer's existing `min_confidence` seam — no app-code change).
        min_confidence = 0.7 if toggles.get("strict_kc_tagging") else 0.5
        return await harness.score_kc_tagging(
            client, harness.load_kc_tagging_cases(), min_confidence=min_confidence
        )
    if suite == "retrieval":
        if session is None:
            raise ValueError("the 'retrieval' suite requires a database session")
        return await harness.score_retrieval(session, client, harness.load_retrieval_cases())
    raise ValueError(f"unknown or non-sweepable suite {suite!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_sweep_runner.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/sweep/runner.py tests/eval/test_sweep_runner.py
git commit -m "feat(9a): cell runner — cost-tracked suites over golden cases"
```

---

### Task 5: Tracker seam — protocol, fake, MLflow

**Files:**
- Create: `tests/eval/sweep/tracking.py`
- Test: `tests/eval/test_sweep_tracking.py`
- Modify: `pyproject.toml` (add `mlflow` dev dep — Step 0), `.gitignore` (add `mlruns/`)

**Interfaces:**
- Consumes: `mlflow` (lazy import in `MLflowTracker`).
- Produces:
  - `RunRecord(BaseModel)`: `name: str`, `params: dict[str, str]`, `metrics: dict[str, float]`.
  - `Tracker(Protocol)`: `start_run(name)`, `log_params(dict)`, `log_metrics(dict)`, `log_artifact(path)`, `end_run()`, `list_runs() -> list[RunRecord]`.
  - `FakeTracker` (in-memory; also exposes `.runs`, `.artifacts`).
  - `MLflowTracker(experiment, tracking_uri=None)` — local file store.

- [ ] **Step 0: Add mlflow dev dep + gitignore mlruns/**

Run: `uv add --group dev mlflow`
Expected: `mlflow` added under `[dependency-groups] dev`; `uv.lock` updates. (This pulls a large tree — that's the accepted experiment-tooling cost, spec §13.)

Append to `.gitignore`:

```
# MLflow local tracking store (experiment sweeps)
mlruns/
```

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_sweep_tracking.py`:

```python
"""FakeTracker capture + a light MLflowTracker file-store round-trip."""

from tests.eval.sweep.tracking import FakeTracker, MLflowTracker


def test_fake_tracker_records_run_lifecycle() -> None:
    t = FakeTracker()
    t.start_run("cell-a")
    t.log_params({"smart_model": "openrouter:claude-opus-4-8"})
    t.log_metrics({"rubric_pass_rate": 0.9})
    t.log_artifact("/tmp/report.json")
    t.end_run()

    runs = t.list_runs()
    assert len(runs) == 1
    assert runs[0].name == "cell-a"
    assert runs[0].params == {"smart_model": "openrouter:claude-opus-4-8"}
    assert runs[0].metrics == {"rubric_pass_rate": 0.9}
    assert t.artifacts["cell-a"] == ["/tmp/report.json"]


def test_mlflow_tracker_logs_and_reads_back(tmp_path) -> None:
    tracker = MLflowTracker("test-exp", tracking_uri=f"file:{tmp_path / 'mlruns'}")
    tracker.start_run("cell-a")
    tracker.log_params({"smart_model": "claude-opus-4-8"})
    tracker.log_metrics({"rubric_pass_rate": 1.0, "cost_usd": 0.5})
    tracker.end_run()

    runs = tracker.list_runs()
    assert len(runs) == 1
    assert runs[0].name == "cell-a"
    assert runs[0].params["smart_model"] == "claude-opus-4-8"
    assert runs[0].metrics["rubric_pass_rate"] == 1.0
    assert runs[0].metrics["cost_usd"] == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_sweep_tracking.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.sweep.tracking'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/sweep/tracking.py`:

```python
"""Pluggable experiment tracking (D7): a Protocol seam, an MLflow impl, an in-memory fake."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    """A logged run read back for reporting: its name, params, and metrics."""

    name: str
    params: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)


class Tracker(Protocol):
    """The tracking seam. One cell = one run."""

    def start_run(self, name: str) -> None: ...
    def log_params(self, params: dict[str, str]) -> None: ...
    def log_metrics(self, metrics: dict[str, float]) -> None: ...
    def log_artifact(self, path: str | Path) -> None: ...
    def end_run(self) -> None: ...
    def list_runs(self) -> list[RunRecord]: ...


class FakeTracker:
    """In-memory tracker for tests: records everything, serves it back via ``list_runs``."""

    def __init__(self) -> None:
        self.runs: list[RunRecord] = []
        self.artifacts: dict[str, list[str]] = {}
        self._current: RunRecord | None = None

    def start_run(self, name: str) -> None:
        self._current = RunRecord(name=name)
        self.runs.append(self._current)
        self.artifacts[name] = []

    def log_params(self, params: dict[str, str]) -> None:
        assert self._current is not None, "log_params outside a run"
        self._current.params.update(params)

    def log_metrics(self, metrics: dict[str, float]) -> None:
        assert self._current is not None, "log_metrics outside a run"
        self._current.metrics.update(metrics)

    def log_artifact(self, path: str | Path) -> None:
        assert self._current is not None, "log_artifact outside a run"
        self.artifacts[self._current.name].append(str(path))

    def end_run(self) -> None:
        self._current = None

    def list_runs(self) -> list[RunRecord]:
        return list(self.runs)


def _present(value: object) -> bool:
    """True unless the value is None or a NaN float (MLflow fills absent cells with NaN)."""
    return value is not None and not (isinstance(value, float) and math.isnan(value))


class MLflowTracker:
    """Logs to a local file-backed MLflow store (no server). Tracking subset only (D7, §6)."""

    def __init__(self, experiment: str, tracking_uri: str | None = None) -> None:
        import mlflow  # lazy: keeps this module importable without mlflow installed

        self._mlflow = mlflow
        mlflow.set_tracking_uri(tracking_uri or f"file:{Path('mlruns').resolve()}")
        mlflow.set_experiment(experiment)
        self._experiment = experiment

    def start_run(self, name: str) -> None:
        self._mlflow.start_run(run_name=name)

    def log_params(self, params: dict[str, str]) -> None:
        self._mlflow.log_params(params)

    def log_metrics(self, metrics: dict[str, float]) -> None:
        self._mlflow.log_metrics(metrics)

    def log_artifact(self, path: str | Path) -> None:
        self._mlflow.log_artifact(str(path))

    def end_run(self) -> None:
        self._mlflow.end_run()

    def list_runs(self) -> list[RunRecord]:
        frame = self._mlflow.search_runs(experiment_names=[self._experiment])
        records: list[RunRecord] = []
        for _, row in frame.iterrows():
            params = {
                key[len("params.") :]: str(val)
                for key, val in row.items()
                if key.startswith("params.") and _present(val)
            }
            metrics = {
                key[len("metrics.") :]: float(val)
                for key, val in row.items()
                if key.startswith("metrics.") and _present(val)
            }
            name = row.get("tags.mlflow.runName") or row.get("run_id", "")
            records.append(RunRecord(name=str(name), params=params, metrics=metrics))
        return records
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_sweep_tracking.py -v`
Expected: PASS (2 passed). (The MLflow test writes to a temp file store; no server needed.)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/sweep/tracking.py tests/eval/test_sweep_tracking.py pyproject.toml uv.lock .gitignore
git commit -m "feat(9a): Tracker seam — protocol, FakeTracker, local-file MLflowTracker"
```

---

### Task 6: Reporting — leaderboard + pairwise diff

**Files:**
- Create: `tests/eval/sweep/report.py` (pure functions only in this task; the CLI `main` is added in Task 8)
- Test: `tests/eval/test_sweep_report.py`

**Interfaces:**
- Consumes: `RunRecord` (Task 5).
- Produces:
  - `leaderboard(runs: list[RunRecord], *, quality_metric: str = "rubric_pass_rate") -> list[RunRecord]` — quality desc, ties broken by `cost_usd` asc; a run missing the quality metric sorts last.
  - `pairwise_diff(a: RunRecord, b: RunRecord) -> dict[str, float]` — per-metric `b − a` over the union of metric keys (missing = 0.0).

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_sweep_report.py`:

```python
"""Leaderboard ordering + pairwise diff math."""

import pytest

from tests.eval.sweep.report import leaderboard, pairwise_diff
from tests.eval.sweep.tracking import RunRecord


def _run(name: str, quality: float, cost: float) -> RunRecord:
    return RunRecord(name=name, metrics={"rubric_pass_rate": quality, "cost_usd": cost})


def test_leaderboard_ranks_by_quality_then_cost() -> None:
    runs = [_run("mid", 0.9, 0.2), _run("top", 1.0, 0.5), _run("cheap-tie", 0.9, 0.1)]
    assert [r.name for r in leaderboard(runs)] == ["top", "cheap-tie", "mid"]


def test_leaderboard_missing_quality_sorts_last() -> None:
    runs = [RunRecord(name="no-metric"), _run("good", 0.8, 0.3)]
    assert [r.name for r in leaderboard(runs)][0] == "good"


def test_pairwise_diff_is_b_minus_a() -> None:
    diff = pairwise_diff(_run("a", 0.8, 0.5), _run("b", 0.95, 0.1))
    assert diff["rubric_pass_rate"] == pytest.approx(0.15)
    assert diff["cost_usd"] == pytest.approx(-0.4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_sweep_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.sweep.report'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/sweep/report.py`:

```python
"""Leaderboard + pairwise ablation diff over logged runs (pure functions)."""

from __future__ import annotations

from tests.eval.sweep.tracking import RunRecord


def leaderboard(
    runs: list[RunRecord], *, quality_metric: str = "rubric_pass_rate"
) -> list[RunRecord]:
    """Runs ranked by quality (desc), ties broken by cost (asc). Missing quality sorts last."""

    def key(run: RunRecord) -> tuple[float, float]:
        quality = run.metrics.get(quality_metric, float("-inf"))
        cost = run.metrics.get("cost_usd", float("inf"))
        return (-quality, cost)

    return sorted(runs, key=key)


def pairwise_diff(a: RunRecord, b: RunRecord) -> dict[str, float]:
    """Per-metric delta ``b − a`` over metrics present in either run (missing = 0.0)."""
    keys = set(a.metrics) | set(b.metrics)
    return {k: b.metrics.get(k, 0.0) - a.metrics.get(k, 0.0) for k in sorted(keys)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_sweep_report.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/sweep/report.py tests/eval/test_sweep_report.py
git commit -m "feat(9a): leaderboard + pairwise ablation diff (pure)"
```

---

### Task 7: Sweep orchestration + `poe sweep` + first config + pricing smoke test

**Files:**
- Create: `tests/eval/sweep/orchestrate.py`, `tests/eval/sweep/__main__.py`, `tests/eval/experiments/role-selection-v1.yaml`
- Test: `tests/eval/test_sweep_orchestrate.py`, `tests/eval/test_sweep_pricing_table.py`
- Modify: `pyproject.toml` (`[tool.poe.tasks]`: add `sweep`)

**Interfaces:**
- Consumes: `expand`/`SweepConfig`/`Cell` (Task 2), `run_cell` (Task 4), `Tracker` (Task 5), `CostSummary` + `harness.EvalReport`.
- Produces: `run_sweep(config: SweepConfig, base_client: LLMClient, tracker: Tracker, *, session: AsyncSession | None = None, artifact_dir: Path | None = None) -> None` — one cell = one tracked run; a failed cell logs `status=failed` and does not abort the sweep.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_sweep_orchestrate.py`:

```python
"""The whole-sweep loop: one run per cell, and a failing cell doesn't abort the matrix."""

from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from tests.eval.sweep.config import SweepConfig
from tests.eval.sweep.orchestrate import run_sweep
from tests.eval.sweep.tracking import FakeTracker


def _fake_base(reply: str) -> LLMClient:
    return LLMClient({"fake": FakeProvider(reply=reply)}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def test_run_sweep_logs_one_ok_run_per_cell(tmp_path) -> None:
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
    tracker = FakeTracker()
    await run_sweep(config, _fake_base('{"score": 1.0, "rationale": "ok"}'), tracker, artifact_dir=tmp_path)

    runs = tracker.list_runs()
    assert len(runs) == 2
    assert all(r.params["status"] == "ok" for r in runs)
    assert all("rubric_pass_rate" in r.metrics and "cost_usd" in r.metrics for r in runs)
    assert runs[0].params["smart_model"] == "openrouter:claude-opus-4-8"
    assert all(len(tracker.artifacts[r.name]) == 1 for r in runs)  # one artifact per suite per cell


async def test_run_sweep_survives_a_failing_cell(tmp_path) -> None:
    # An unknown suite makes the cell raise; the sweep logs it failed and continues.
    config = SweepConfig.model_validate(
        {"name": "s", "suites": ["bogus"], "axes": {"smart": [{"provider": "p", "model": "m"}]}}
    )
    tracker = FakeTracker()
    await run_sweep(config, _fake_base("x"), tracker, artifact_dir=tmp_path)

    runs = tracker.list_runs()
    assert len(runs) == 1
    assert runs[0].params["status"] == "failed"
    assert "rubric_pass_rate" not in runs[0].metrics
```

Create `tests/eval/test_sweep_pricing_table.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/eval/test_sweep_orchestrate.py tests/eval/test_sweep_pricing_table.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.sweep.orchestrate'` (orchestrate test) and no `experiments/` configs (pricing test errors/collects nothing).

- [ ] **Step 3: Write the orchestration + config + CLI**

Create `tests/eval/sweep/orchestrate.py`:

```python
"""Drive a whole sweep: expand → run each cell → log params/metrics/artifacts per run."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from tests.eval import harness
from tests.eval.sweep.config import Cell, SweepConfig, expand
from tests.eval.sweep.cost import CostSummary
from tests.eval.sweep.runner import run_cell
from tests.eval.sweep.tracking import Tracker


async def run_sweep(
    config: SweepConfig,
    base_client: LLMClient,
    tracker: Tracker,
    *,
    session: AsyncSession | None = None,
    artifact_dir: Path | None = None,
) -> None:
    """Run every cell in ``config``; one cell = one tracked run. A failed cell is logged with
    ``status=failed`` and does not abort the sweep (§10)."""
    for cell in expand(config):
        tracker.start_run(cell.id)
        tracker.log_params(_cell_params(cell))
        try:
            reports, cost = await run_cell(cell, base_client, session=session)
        except Exception as exc:  # provider error / timeout / auth / bad suite — keep going
            tracker.log_params({"status": "failed", "error": str(exc)[:500]})
            tracker.end_run()
            continue
        tracker.log_params({"status": "ok"})
        tracker.log_metrics(_cell_metrics(reports, cost))
        _log_artifacts(tracker, cell, reports, artifact_dir)
        tracker.end_run()


def _cell_params(cell: Cell) -> dict[str, str]:
    params: dict[str, str] = {"suites": ",".join(cell.suites)}
    for role, spec in cell.role_overrides.items():
        params[f"{role.value}_model"] = f"{spec.provider}:{spec.model}"
    for name, value in cell.toggles.items():
        params[f"toggle_{name}"] = str(value)
    for key, value in cell.gen_config.items():
        params[f"gen_{key}"] = str(value)
    return params


def _cell_metrics(reports: list[harness.EvalReport], cost: CostSummary) -> dict[str, float]:
    metrics: dict[str, float] = {
        "cost_usd": cost.cost_usd,
        "total_tokens": float(cost.total_tokens),
    }
    for report in reports:
        metrics[f"{report.suite}_pass_rate"] = report.pass_rate
        if report.mae is not None:
            metrics[f"{report.suite}_mae"] = report.mae
    return metrics


def _log_artifacts(
    tracker: Tracker,
    cell: Cell,
    reports: list[harness.EvalReport],
    artifact_dir: Path | None,
) -> None:
    out = artifact_dir or Path(tempfile.mkdtemp())
    out.mkdir(parents=True, exist_ok=True)
    for report in reports:
        path = out / f"{cell.id}-{report.suite}.json"
        path.write_text(report.model_dump_json(indent=2))
        tracker.log_artifact(path)
```

Create `tests/eval/experiments/role-selection-v1.yaml`:

```yaml
name: role-selection-v1
suites: [rubric]          # rubric exercises the SMART role
axes:
  smart:                  # candidate models for the SMART role
    - {provider: openrouter, model: claude-opus-4-8}
    - {provider: openrouter, model: claude-sonnet-4-6}
    - {provider: ollama,     model: llama3.1:8b}   # adjust to a model you have pulled
gen_config:
  - {}
toggles: {}
```

Create `tests/eval/sweep/__main__.py`:

```python
"""`poe sweep <config.yaml>` — run a live sweep, logging to MLflow. Paid/slow/manual.

Not part of `poe check`/`poe eval`. Requires provider credentials (OpenRouter for frontier
models, a running Ollama for OSS). The `retrieval` suite additionally needs a DB session, which
this CLI does not yet wire (role-selection-v1 is rubric-only); such a cell logs `status=failed`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.llm.registry import build_llm_client
from tests.eval.sweep.config import load_sweep_config
from tests.eval.sweep.orchestrate import run_sweep
from tests.eval.sweep.tracking import MLflowTracker


async def _run(config_path: str) -> None:
    config = load_sweep_config(config_path)
    base_client = build_llm_client(get_settings())
    tracker = MLflowTracker(experiment=config.name)
    artifact_dir = Path("mlruns") / "artifacts" / config.name
    await run_sweep(config, base_client, tracker, artifact_dir=artifact_dir)
    print(f"sweep '{config.name}' complete — view with: uv run mlflow ui")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: poe sweep <config.yaml>", file=sys.stderr)
        return 2
    asyncio.run(_run(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/eval/test_sweep_orchestrate.py tests/eval/test_sweep_pricing_table.py -v`
Expected: PASS (3 passed). (Pricing test: `opus-4-8` + `sonnet-4-6` are in `_PRICES`; the `ollama` candidate is skipped.)

- [ ] **Step 5: Wire `poe sweep`**

In `pyproject.toml`, under `[tool.poe.tasks]`, add after the `eval` line:

```toml
sweep = "python -m tests.eval.sweep"
```

Verify wiring (no live run — bad usage prints and exits 2):

Run: `uv run poe sweep 2>&1 | head -1`
Expected: `usage: poe sweep <config.yaml>`

- [ ] **Step 6: Commit**

```bash
git add tests/eval/sweep/orchestrate.py tests/eval/sweep/__main__.py tests/eval/experiments/role-selection-v1.yaml tests/eval/test_sweep_orchestrate.py tests/eval/test_sweep_pricing_table.py pyproject.toml
git commit -m "feat(9a): sweep orchestration + poe sweep + role-selection-v1 config"
```

---

### Task 8: Report CLI + `poe sweep-report`

**Files:**
- Modify: `tests/eval/sweep/report.py` (add `main` + formatting helpers)
- Test: `tests/eval/test_sweep_report.py` (extend)
- Modify: `pyproject.toml` (`[tool.poe.tasks]`: add `sweep-report`)

**Interfaces:**
- Consumes: `leaderboard`, `pairwise_diff` (Task 6), `RunRecord`, `MLflowTracker` (Task 5).
- Produces: `_format_leaderboard(runs) -> str`, `_format_diff(a, b) -> str`, `main() -> int` (argparse: `--experiment`, optional `--compare RUN_A RUN_B`).

- [ ] **Step 1: Write the failing test (extend the report test file)**

Append to `tests/eval/test_sweep_report.py`:

```python
from tests.eval.sweep.report import _format_diff, _format_leaderboard


def test_format_leaderboard_orders_best_first() -> None:
    out = _format_leaderboard([_run("mid", 0.9, 0.2), _run("top", 1.0, 0.5)])
    body = [line for line in out.splitlines() if line.startswith(("top", "mid"))]
    assert body[0].startswith("top") and body[1].startswith("mid")


def test_format_diff_shows_signed_deltas() -> None:
    out = _format_diff(_run("a", 0.8, 0.5), _run("b", 0.95, 0.1))
    assert "b" in out and "a" in out
    assert "+0.1500" in out  # rubric_pass_rate delta
    assert "-0.4000" in out  # cost_usd delta
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_sweep_report.py -v`
Expected: FAIL — `ImportError: cannot import name '_format_diff'`

- [ ] **Step 3: Add the formatting helpers + CLI to `report.py`**

At the top of `tests/eval/sweep/report.py`, add `import sys` under `from __future__ import annotations`. Then append:

```python
def _format_leaderboard(runs: list[RunRecord]) -> str:
    ranked = leaderboard(runs)
    lines = [f"{'run':<28}{'quality':>9}{'cost_usd':>12}", "-" * 49]
    for run in ranked:
        quality = run.metrics.get("rubric_pass_rate", float("nan"))
        cost = run.metrics.get("cost_usd", float("nan"))
        lines.append(f"{run.name:<28}{quality:>9.3f}{cost:>12.4f}")
    return "\n".join(lines)


def _format_diff(a: RunRecord, b: RunRecord) -> str:
    lines = [f"diff: {b.name} - {a.name}", "-" * 34]
    lines += [f"{key:<24}{delta:>+10.4f}" for key, delta in pairwise_diff(a, b).items()]
    return "\n".join(lines)


def main() -> int:
    import argparse

    from tests.eval.sweep.tracking import MLflowTracker

    parser = argparse.ArgumentParser(prog="sweep-report")
    parser.add_argument("--experiment", help="experiment name to report on")
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"))
    args = parser.parse_args()

    if not args.experiment:
        print(
            "usage: poe sweep-report --experiment <name> [--compare RUN_A RUN_B]",
            file=sys.stderr,
        )
        return 2

    runs = MLflowTracker(experiment=args.experiment).list_runs()
    if args.compare:
        by_name = {r.name: r for r in runs}
        a, b = by_name.get(args.compare[0]), by_name.get(args.compare[1])
        if a is None or b is None:
            print(f"run(s) not found: {args.compare}", file=sys.stderr)
            return 1
        print(_format_diff(a, b))
    else:
        print(_format_leaderboard(runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_sweep_report.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Wire `poe sweep-report`**

In `pyproject.toml`, under `[tool.poe.tasks]`, add after the `sweep` line:

```toml
sweep-report = "python -m tests.eval.sweep.report"
```

Verify wiring:

Run: `uv run poe sweep-report 2>&1 | head -1`
Expected: `usage: poe sweep-report --experiment <name> [--compare RUN_A RUN_B]`

- [ ] **Step 6: Commit**

```bash
git add tests/eval/sweep/report.py tests/eval/test_sweep_report.py pyproject.toml
git commit -m "feat(9a): sweep-report CLI — leaderboard + pairwise ablation diff"
```

---

### Final gate + live walkthrough

- [ ] **Step 1: Full green gate**

Run: `uv run poe check`
Expected: lint clean, ty clean, all tests pass (the pre-existing suite plus the new sweep tests). `poe eval` is untouched.

- [ ] **Step 2: Confirm `poe eval` still behaves exactly as before**

Run: `uv run poe eval`
Expected: the two deterministic suites print and exit 0 — unchanged by this work.

- [ ] **Step 3: Live walkthrough (manual — the DoD proof; paid, not CI)**

This runs real models and costs money; do it once to prove the end-to-end path. Not part of any gate.

1. Ensure the Ollama model named in `role-selection-v1.yaml` is pulled (`ollama pull llama3.1:8b`, or edit the config to a model you have) and that OpenRouter credentials are configured in settings (`GURU_OPENROUTER_API_KEY`).
2. Run the sweep:
   ```bash
   uv run poe sweep tests/eval/experiments/role-selection-v1.yaml
   ```
   Expected: 3 runs logged (one per SMART candidate), each with `rubric_pass_rate`, `cost_usd`, `total_tokens`; the two OpenRouter cells carry non-zero cost, the Ollama cell `$0`.
3. Print the leaderboard:
   ```bash
   uv run poe sweep-report --experiment role-selection-v1
   ```
   Expected: 3 cells ranked by rubric quality with a cost column — the "SMART=X reaches Y% of opus's rubric quality at Z% cost" statement is now readable off the table.
4. Optional pairwise diff + UI:
   ```bash
   uv run poe sweep-report --experiment role-selection-v1 --compare role-selection-v1-000 role-selection-v1-001
   uv run mlflow ui   # browse/compare runs at http://localhost:5000
   ```

---

## Notes for the implementer

- **Namespace package:** do NOT add `tests/__init__.py` or `tests/eval/sweep/__init__.py` — the repo relies on implicit namespace packages (that's why `python -m tests.eval.harness` already works). Adding `__init__.py` would change pytest's import mode and can break the existing `import harness` in `test_eval.py`.
- **Import direction:** sweep modules import the harness as `from tests.eval import harness` (or `from tests.eval.harness import ...`), never bare `import harness` — the bare form only resolves under pytest, not under `python -m`.
- **`ty` and single-underscore access:** `CostTrackingClient.__init__` reads `inner._providers` / `inner._roles` (a subclass reading its own base's fields via the passed instance). This is intentional and ty-clean; do not add public accessors to `LLMClient` for it.
- **Retrieval live-sweep:** `run_cell` fully supports `retrieval` when handed a `session` (covered by the runner unit test path), but the `poe sweep` CLI does not yet build a DB session — `role-selection-v1` is rubric-only. Wiring a session into the CLI is a clean later addition, not part of this plan.
