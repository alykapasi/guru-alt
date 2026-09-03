# Phase 9c — Runtime DSPy Prompt Optimization (kc_tagging) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce DSPy as a runtime prompt-optimization substrate behind a thin `app/prompts/` seam and convert `kc_tagging` into a runtime DSPy program whose compiled artifact (optimized instruction + few-shot demos) ships in git, with an offline compile step and a measured delta-vs-hand-written report.

**Architecture:** DSPy is reached only through `app/prompts/` — a `RoleLM(dspy.BaseLM)` routes every DSPy model call to our role-based `LLMClient` (never litellm/a provider SDK). `kc_tagging`'s `tag_chunk` keeps its exact public API but internally runs a `dspy.Predict` program loaded from a committed artifact (uncompiled fallback if absent). Offline tooling under `tests/eval/prompts/` compiles the program with `BootstrapFewShot` and reports the baseline-vs-compiled delta on a held-out dev split, reusing the 9a MLflow `Tracker`.

**Tech Stack:** Python 3.13, DSPy (new runtime dep, pinned), Pydantic v2, SQLAlchemy 2 async, pytest (asyncio_mode=auto), the existing `app.llm` registry + `tests/eval` harness + 9a `tests/eval/sweep/tracking`.

**Spec:** `docs/superpowers/specs/2026-09-03-dspy-prompt-optimization-design.md`.

## Global Constraints

- **Python ≥ 3.13.** ruff line-length 100 (E501 ignored); `uv run poe check` (lint + type-check + test) must end green at the end of every code task.
- **DSPy reaches a model ONLY via `RoleLM` → `LLMClient` by role.** No `import litellm`, no provider SDK, no hardcoded model name anywhere in `app/` (D4). `kc_tagging` uses `ModelRole.FAST`.
- **`app/prompts/` is the only place that imports `dspy`.** `app/learning/kc_tagging.py` imports from `app.prompts`, never `dspy` directly. The offline `tests/eval/prompts/` tooling may import `dspy`.
- **`tag_chunk`'s public contract is unchanged:** `async def tag_chunk(client, text, candidates, *, min_confidence=0.5, max_tokens=256) -> tuple[list[KCTag], Usage]`; best-effort (any failure → `[]`, never raises); no candidates → no model call → `([], Usage())`; per-call token `Usage` still returned for cost logging.
- **Compiled artifact is committed** at `app/prompts/artifacts/kc_tagging.json` (state-only JSON, `save_program=False`), produced by the manual compile step (D6). Runtime falls back to an **uncompiled** program (seeded with the current hand-written instruction, zero demos) when the file is absent/corrupt — CI never has a compiled artifact and must stay green on the fallback.
- **`compile` and `report` are paid/manual** (need a live model); they are NOT part of `poe check` or `poe eval`.
- **Offline CI tests use a fake client** (`app.llm.registry.fake_llm_client`) — no live model. The fake's reply text must be in the shape DSPy's adapter parses into the signature's output field (pinned by Task 1).
- **`tests/eval/prompts/` is a namespace package** — NO `__init__.py` (like `tests/eval/sweep/` and `tests/eval/datasets/`); absolute imports (`from tests.eval.prompts.<mod> import …`). **`app/prompts/` IS a regular package** — it needs `__init__.py` (it lives under beartype-claw'd `app/`).
- **Optimizer = `dspy.BootstrapFewShot`; module = `dspy.Predict`** (no `ChainOfThought`, no `MIPROv2`) (D7).
- **Git discipline:** one commit per task on top of the current HEAD; stage only that task's files (never `git add -A`); keep `.claude/settings.json` untracked; do not `reset`/`amend`/`rebase` existing commits. If a pre-commit hook reformats, re-`git add` only your task's files and re-commit.

## Verified DSPy API facts (from the docs, 2026-09; Task 1 re-confirms at the pinned version)

- **Custom LM:** subclass `dspy.BaseLM`; `__init__(self, model, model_type='chat', temperature=None, max_tokens=None, cache=True, callbacks=None, num_retries=3, **kwargs)` — `model` is a required positional (pass a synthetic id like `"role:fast"`; the real model is resolved by our registry). Contract is mid-migration: **legacy** (`forward_contract = "legacy"`, the default) implements `forward(self, prompt=None, messages=None, **kwargs)` and `aforward(...)` returning an **OpenAI chat-completion-shaped** object (`.choices[0].message.content`, `.usage.prompt_tokens/.completion_tokens/.total_tokens`, `.model`). Register per-call with `dspy.context(lm=...)`.
- **Program run:** sync `program(**inputs)` calls `forward`; async `await program.acall(**inputs)` calls `aforward` (Task 1 verifies `acall`→`aforward` is clean at the pinned version — this is the D5 async-first path).
- **Optimizer:** `dspy.BootstrapFewShot(metric=…, max_bootstrapped_demos=4, max_labeled_demos=16, max_rounds=1).compile(student, *, teacher=None, trainset=…)`.
- **Metric:** a callable `(example, prediction, trace=None) -> bool | float`.
- **Save/load (state-only JSON):** `program.save("path.json", save_program=False)`; reload by constructing the program then `program.load("path.json")`.

---

### Task 1: Add DSPy dep + pin-and-verify the custom-LM contract

**Files:**
- Modify: `pyproject.toml` (add `dspy` to `[project].dependencies`)
- Create: `tests/test_dspy_contract.py`

**Interfaces:**
- Consumes: `dspy` (new dep).
- Produces: a committed regression test that documents the exact working legacy-`BaseLM` return shape + the exact fake-reply format DSPy's adapter parses. Later tasks (2, 3) reuse both.

**Why a spike:** DSPy's custom-LM contract is mid-migration (legacy vs typed) and typed-output parsing is adapter-version-specific. This task pins the version and locks the two facts every later task depends on: (a) the legacy `BaseLM` response object shape that DSPy accepts, and (b) the reply text that the adapter parses into an output field.

- [ ] **Step 1: Add the dependency**

Run: `uv add dspy` (resolves + pins the latest stable into `pyproject.toml` + `uv.lock`). Record the resolved version in the commit message.

- [ ] **Step 2: Write the contract-verification test**

Create `tests/test_dspy_contract.py`. It builds a minimal legacy `BaseLM` returning a hand-constructed OpenAI-shaped object, runs a `dspy.Predict` through it, and asserts DSPy parses the output — pinning both the response shape and the fake-reply format:

```python
"""Pins the DSPy custom-LM legacy contract + adapter reply shape this codebase relies on.

If a dspy upgrade breaks this, RoleLM (app/prompts/lm.py) and the kc_tagging program must be
revisited. Runs fully offline — the fake LM returns canned text, no network.
"""

import types

import dspy


class _FakeLM(dspy.BaseLM):
    forward_contract = "legacy"

    def __init__(self, reply: str) -> None:
        super().__init__(model="fake:test")
        self._reply = reply

    def forward(self, prompt=None, messages=None, **kwargs):
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=self._reply))],
            usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="fake:test",
        )

    async def aforward(self, prompt=None, messages=None, **kwargs):
        return self.forward(prompt=prompt, messages=messages, **kwargs)


def test_legacy_baselm_predict_parses_output() -> None:
    # A single-output signature; the reply is the adapter's expected field format for `answer`.
    lm = _FakeLM(reply="[[ ## answer ## ]]\nblue\n\n[[ ## completed ## ]]")
    with dspy.context(lm=lm):
        pred = dspy.Predict("question -> answer")(question="What color is the sky?")
    assert pred.answer.strip() == "blue"


async def test_legacy_baselm_async_acall_parses_output() -> None:
    lm = _FakeLM(reply="[[ ## answer ## ]]\ngreen\n\n[[ ## completed ## ]]")
    with dspy.context(lm=lm):
        pred = await dspy.Predict("question -> answer").acall(question="What color is grass?")
    assert pred.answer.strip() == "green"
```

- [ ] **Step 3: Run the test — verify the pinned contract**

Run: `uv run pytest tests/test_dspy_contract.py -v`

Expected: both pass. **If the sync test fails on the response shape**, adjust `_FakeLM.forward`'s returned object to the exact shape the pinned version accepts (a dict, a litellm `ModelResponse`, or the typed `dspy.LMResponse` with `forward_contract = "typed_lm"`) until it passes — this is the shape RoleLM must return in Task 2. **If the reply-format assertion fails**, replace the `[[ ## answer ## ]]…` string with the exact format the pinned adapter parses (try a bare `"blue"` or a JSON `{"answer": "blue"}`) until it passes — this is the fake-reply format later CI tests reuse. **If `acall`→`aforward` is not clean** (raises / never calls `aforward`), delete `test_legacy_baselm_async_acall_parses_output`, add a comment `# async acall path unavailable at dspy X.Y — Task 4 uses the threadpool bridge (Approach B)`, and record this — Task 4 branches on it.

- [ ] **Step 4: Confirm the whole suite still imports/passes**

Run: `uv run poe check`
Expected: green (adding dspy must not break existing imports).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/test_dspy_contract.py
git commit -m "feat(9c): add dspy dep + pin custom-LM legacy contract (dspy <version>)"
```

---

### Task 2: `RoleLM` — the DSPy→LLMClient seam

**Files:**
- Create: `app/prompts/__init__.py` (empty), `app/prompts/lm.py`
- Test: `tests/test_prompts_lm.py`

**Interfaces:**
- Consumes: `dspy.BaseLM` (contract pinned in Task 1); `app.llm.LLMClient`, `app.llm.ModelRole`, `app.llm.ChatMessage`, `app.llm.ChatRole`, `app.llm.Usage`; `app.llm.registry.fake_llm_client` (tests).
- Produces: `class RoleLM(dspy.BaseLM)` with `__init__(self, role: ModelRole, client: LLMClient, *, max_tokens: int = 1024)`, attribute `usage_sum: Usage` accumulating token usage across calls, `async def aforward(...)` (runtime), `def forward(...)` (offline sync compile). Returns the Task-1 response shape.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prompts_lm.py` (uses the fake client so no live model; `fake_llm_client(reply=...)` returns an `LLMClient` whose `complete` yields `reply`):

```python
"""RoleLM routes DSPy calls through our role-based LLMClient (Phase 9c)."""

import dspy

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.prompts.lm import RoleLM


def test_forward_returns_openai_shaped_reply_and_records_usage() -> None:
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply="hello world"))
    resp = lm.forward(messages=[{"role": "user", "content": "hi"}])
    assert resp.choices[0].message.content == "hello world"
    assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens
    assert lm.usage_sum.total_tokens > 0  # usage accumulated for cost logging


async def test_aforward_matches_forward() -> None:
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply="async reply"))
    resp = await lm.aforward(messages=[{"role": "user", "content": "hi"}])
    assert resp.choices[0].message.content == "async reply"


def test_predict_runs_through_rolelm() -> None:
    # End-to-end: a DSPy Predict driven by RoleLM over a fake client that returns the
    # Task-1-confirmed adapter reply format for a single `answer` field.
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply="[[ ## answer ## ]]\n42\n\n[[ ## completed ## ]]"))
    with dspy.context(lm=lm):
        pred = dspy.Predict("question -> answer")(question="6x7?")
    assert pred.answer.strip() == "42"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prompts_lm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.prompts'`.

- [ ] **Step 3: Write the implementation**

Create `app/prompts/__init__.py` (empty). Create `app/prompts/lm.py`. Translate DSPy's OpenAI-style `messages` (list of `{"role","content"}`) into our `ChatMessage`s (a leading `system` message becomes the `system=` arg); call `client.complete`; return the Task-1 shape. Use the `types.SimpleNamespace` shape confirmed in Task 1 (adjust if Task 1 pinned a different shape):

```python
"""RoleLM: the only bridge between DSPy and our role-based LLMClient (Phase 9c, D4).

DSPy reaches a model exclusively through this adapter — never litellm, never a provider SDK.
Bound to a ModelRole; the concrete model is resolved by the registry. Async-first (aforward) for
the runtime ingestion path; a sync forward() supports the offline BootstrapFewShot compile.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Sequence

import dspy

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage


class RoleLM(dspy.BaseLM):
    forward_contract = "legacy"

    def __init__(self, role: ModelRole, client: LLMClient, *, max_tokens: int = 1024) -> None:
        super().__init__(model=f"role:{role.value}", max_tokens=max_tokens)
        self._role = role
        self._client = client
        self._max_tokens = max_tokens
        self.usage_sum = Usage()

    async def aforward(self, prompt=None, messages=None, **kwargs):
        system, chat = _split_messages(messages, prompt)
        resp = await self._client.complete(
            self._role, chat, system=system, max_tokens=self._max_tokens
        )
        self.usage_sum = Usage(
            input_tokens=self.usage_sum.input_tokens + resp.usage.input_tokens,
            output_tokens=self.usage_sum.output_tokens + resp.usage.output_tokens,
        )
        return _openai_response(resp.content, resp.model, resp.usage)

    def forward(self, prompt=None, messages=None, **kwargs):
        # Offline compile is synchronous with no running loop; drive the async client directly.
        return asyncio.run(self.aforward(prompt=prompt, messages=messages, **kwargs))


def _split_messages(
    messages: list[dict] | None, prompt: str | None
) -> tuple[str | None, list[ChatMessage]]:
    if not messages:
        return None, [ChatMessage(role=ChatRole.USER, content=prompt or "")]
    system: str | None = None
    chat: list[ChatMessage] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "system":
            system = content if system is None else f"{system}\n{content}"
        else:
            chat.append(ChatMessage(role=ChatRole(role), content=content))
    return system, chat


def _openai_response(content: str, model: str, usage: Usage):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=None))
        ],
        usage=types.SimpleNamespace(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        ),
        model=model,
    )
```

Note: `_split_messages`'s parameter `Sequence` import is unused — drop the import if ruff flags it. If Task 1 pinned a non-`SimpleNamespace` response shape, make `_openai_response` return that shape instead (keeping the same field values).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prompts_lm.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/prompts/__init__.py app/prompts/lm.py tests/test_prompts_lm.py
git commit -m "feat(9c): RoleLM — DSPy calls routed through the role-based LLMClient"
```

---

### Task 3: The kc_tagging DSPy program + artifact loader

**Files:**
- Create: `app/prompts/kc_tagging_program.py`, `app/prompts/artifacts/.gitkeep`
- Test: `tests/test_kc_tagging_program.py`

**Interfaces:**
- Consumes: `dspy`; `RoleLM` (Task 2); `app.llm.registry.fake_llm_client`, `app.llm.ModelRole` (tests).
- Produces:
  - `KC_TAGGING_INSTRUCTION: str` — the current hand-written instruction (copied verbatim from `app/learning/kc_tagging.py::_SYSTEM_PROMPT`), used as the uncompiled program's instruction.
  - `class TagPassage(dspy.Signature)` — inputs `passage: str`, `candidates: str`; output `tags: list[TagPrediction]` where `TagPrediction(BaseModel)` has `kc: int`, `confidence: float`.
  - `class KCTaggingProgram(dspy.Module)` wrapping `dspy.Predict(TagPassage)`; `forward`/`aforward` delegate to the predictor.
  - `ARTIFACT_PATH: Path` = `app/prompts/artifacts/kc_tagging.json`.
  - `load_kc_tagging_program() -> KCTaggingProgram` — loads the committed artifact if present/valid, else returns an uncompiled program; never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_kc_tagging_program.py`:

```python
"""kc_tagging DSPy program + artifact loader (Phase 9c)."""

import dspy

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.prompts.kc_tagging_program import (
    KCTaggingProgram,
    TagPrediction,
    load_kc_tagging_program,
)
from app.prompts.lm import RoleLM


def test_load_falls_back_to_uncompiled_when_artifact_absent() -> None:
    # No committed artifact in CI -> a usable uncompiled program, no raise.
    program = load_kc_tagging_program()
    assert isinstance(program, KCTaggingProgram)


def test_program_runs_and_yields_tag_predictions() -> None:
    reply = '[[ ## tags ## ]]\n[{"kc": 1, "confidence": 0.9}]\n\n[[ ## completed ## ]]'
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply=reply))
    with dspy.context(lm=lm):
        pred = KCTaggingProgram()(passage="Photosynthesis...", candidates="1. Photosynthesis")
    assert isinstance(pred.tags, list)
    assert pred.tags and isinstance(pred.tags[0], TagPrediction)
    assert pred.tags[0].kc == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kc_tagging_program.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.prompts.kc_tagging_program'`.

- [ ] **Step 3: Write the implementation**

Create `app/prompts/artifacts/.gitkeep` (empty — keeps the dir in git before any compiled artifact exists). Create `app/prompts/kc_tagging_program.py`:

```python
"""The kc_tagging classification as a DSPy program (Phase 9c).

Runtime loads the committed compiled artifact (optimized instruction + few-shot demos); if it is
absent or unreadable, an uncompiled program seeded with the original hand-written instruction runs
instead — so ingestion always has a working tagger. This module is the ONLY user of dspy in the
kc_tagging path; app/learning/kc_tagging.py talks to it, not to dspy.
"""

from __future__ import annotations

from pathlib import Path

import dspy
import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)

KC_TAGGING_INSTRUCTION = (
    "You label a passage with the knowledge components (KCs) it actually teaches. You are given "
    "a numbered list of candidate KCs and a passage. Choose only the KCs the passage directly "
    "teaches or assesses — usually zero to three; omit tangential mentions. Return the candidate "
    "number and your confidence (0.0-1.0) for each chosen KC; return an empty list if none apply."
)

ARTIFACT_PATH = Path(__file__).parent / "artifacts" / "kc_tagging.json"


class TagPrediction(BaseModel):
    kc: int
    confidence: float


class TagPassage(dspy.Signature):
    """Label a passage with the candidate KCs it teaches."""

    passage: str = dspy.InputField()
    candidates: str = dspy.InputField(desc="numbered candidate KCs, one per line")
    tags: list[TagPrediction] = dspy.OutputField(desc="chosen candidate numbers with confidence")


class KCTaggingProgram(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.tag = dspy.Predict(TagPassage.with_instructions(KC_TAGGING_INSTRUCTION))

    def forward(self, passage: str, candidates: str):
        return self.tag(passage=passage, candidates=candidates)

    async def aforward(self, passage: str, candidates: str):
        return await self.tag.acall(passage=passage, candidates=candidates)


def load_kc_tagging_program() -> KCTaggingProgram:
    program = KCTaggingProgram()
    if ARTIFACT_PATH.exists():
        try:
            program.load(str(ARTIFACT_PATH))
        except Exception as exc:  # corrupt/incompatible artifact — fall back, never fail ingest
            log.warning("kc_tagging.artifact_load_failed", path=str(ARTIFACT_PATH), error=str(exc))
    return program
```

Note: if Task 1 showed `acall`→`aforward` is unavailable, `KCTaggingProgram.aforward` can't rely on `self.tag.acall`; leave `forward` (sync) as the working path and Task 4 will use the threadpool bridge. If `TagPassage.with_instructions(...)` isn't the exact method at the pinned version, use the documented equivalent to attach the instruction string to the signature.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kc_tagging_program.py -v`
Expected: PASS (2 passed). If the reply-format string differs from what Task 1 pinned for a typed `list` output field, adjust the `reply` in the test to that format.

- [ ] **Step 5: Commit**

```bash
git add app/prompts/kc_tagging_program.py app/prompts/artifacts/.gitkeep tests/test_kc_tagging_program.py
git commit -m "feat(9c): kc_tagging DSPy program + committed-artifact loader with fallback"
```

---

### Task 4: Rewire `tag_chunk` to the DSPy program

**Files:**
- Modify: `app/learning/kc_tagging.py` (replace the hand-written prompt + `client.complete` call with the DSPy program; keep everything else)
- Test: `tests/test_kc_tagging.py` (existing — extend/adjust so the same behaviors are asserted through the DSPy path)

**Interfaces:**
- Consumes: `load_kc_tagging_program`, `KCTaggingProgram` (Task 3); `RoleLM` (Task 2); existing `KCCandidate`, `KCTag`, `Usage`.
- Produces: unchanged `async def tag_chunk(client, text, candidates, *, min_confidence=0.5, max_tokens=256) -> tuple[list[KCTag], Usage]`.

- [ ] **Step 1: Write/adjust the failing test**

In `tests/test_kc_tagging.py`, ensure these behaviors are asserted through the DSPy path (the fake reply is the Task-1/Task-3 adapter format for the `tags` list field). Keep any existing cases that still hold; add/adjust:

```python
import dspy

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.learning.kc_tagging import KCCandidate, tag_chunk


async def test_tag_chunk_parses_and_thresholds_via_dspy() -> None:
    reply = '[[ ## tags ## ]]\n[{"kc": 1, "confidence": 0.9}, {"kc": 2, "confidence": 0.3}]\n\n[[ ## completed ## ]]'
    cands = [KCCandidate(id=__import__("uuid").uuid4(), name=n) for n in ("Photosynthesis", "Tectonics")]
    tags, usage = await tag_chunk(fake_llm_client(reply=reply), "text", cands, min_confidence=0.5)
    assert [t.kc_id for t in tags] == [cands[0].id]  # 0.3 dropped by threshold; candidate order
    assert usage.total_tokens > 0


async def test_tag_chunk_no_candidates_makes_no_call() -> None:
    tags, usage = await tag_chunk(fake_llm_client(reply="unused"), "text", [])
    assert tags == [] and usage.total_tokens == 0


async def test_tag_chunk_garbage_reply_yields_no_tags() -> None:
    tags, _ = await tag_chunk(
        fake_llm_client(reply="not valid adapter output"),
        "text",
        [KCCandidate(id=__import__("uuid").uuid4(), name="Photosynthesis")],
    )
    assert tags == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kc_tagging.py -v`
Expected: FAIL (still the old prompt path / new assertions unmet).

- [ ] **Step 3: Rewrite `tag_chunk`'s body**

Keep `KCCandidate`, `KCTag`, `TAGGING_ROLE`, `_build_candidate_catalog`, `_clamp`, `load_candidate_kcs`, and the threshold/dedup/order logic. Replace the model-call section. The `_SYSTEM_PROMPT`/`_build_prompt`/`_parse_tags`/`_extract_json` that only served the old raw-JSON path are removed (their intent now lives in the DSPy program + the mapping below). New body:

```python
from app.llm import LLMClient, ModelRole, Usage
from app.prompts.kc_tagging_program import load_kc_tagging_program
from app.prompts.lm import RoleLM

TAGGING_ROLE = ModelRole.FAST


async def tag_chunk(
    client: LLMClient,
    text: str,
    candidates: Sequence[KCCandidate],
    *,
    min_confidence: float = 0.5,
    max_tokens: int = 256,
) -> tuple[list[KCTag], Usage]:
    """Tag one chunk against ``candidates`` with the FAST model via the DSPy program.

    Best-effort: no candidates ⇒ no call; any DSPy/exec/parse failure ⇒ no tags (never raises).
    Returns surviving tags (confidence ≥ ``min_confidence``, de-duped keeping the highest, in
    candidate order) plus the call's ``Usage`` for cost logging.
    """
    if not candidates:
        return [], Usage()
    lm = RoleLM(TAGGING_ROLE, client, max_tokens=max_tokens)
    catalog = "\n".join(
        f"{i}. {c.name}{f' — {c.description}' if c.description else ''}"
        for i, c in enumerate(candidates, start=1)
    )
    program = load_kc_tagging_program()
    try:
        import dspy

        with dspy.context(lm=lm):
            prediction = await program.acall(passage=text, candidates=catalog)
        raw = list(prediction.tags)
    except Exception:  # best-effort — a weak model/parse failure never fails the ingest
        return [], lm.usage_sum
    return _surviving_tags(raw, candidates, min_confidence), lm.usage_sum


def _surviving_tags(raw, candidates, min_confidence: float) -> list[KCTag]:
    best: dict[uuid.UUID, float] = {}
    for entry in raw:
        try:
            index = int(entry.kc)
            confidence = _clamp(float(entry.confidence))
        except (AttributeError, TypeError, ValueError):
            continue
        if not 1 <= index <= len(candidates) or confidence < min_confidence:
            continue
        kc_id = candidates[index - 1].id
        best[kc_id] = max(best.get(kc_id, 0.0), confidence)
    return [KCTag(kc_id=c.id, confidence=best[c.id]) for c in candidates if c.id in best]
```

If Task 1 found `acall`→`aforward` unavailable, replace the `async with dspy.context(...)` block with the **threadpool bridge (Approach B)**: capture the running loop, run the sync program in a thread whose `RoleLM.forward` submits coroutines back via `run_coroutine_threadsafe`:

```python
        loop = asyncio.get_running_loop()

        def _run() -> list:
            with dspy.context(lm=RoleLM(TAGGING_ROLE, client, max_tokens=max_tokens, loop=loop)):
                return list(program(passage=text, candidates=catalog).tags)

        raw = await asyncio.to_thread(_run)
```

(with `RoleLM.forward` using `asyncio.run_coroutine_threadsafe(self.aforward(...), self._loop).result()` when a `loop` was supplied — add that optional `loop` param in Task 2 only if Task 1 requires this branch).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kc_tagging.py -v`
Expected: PASS.

- [ ] **Step 5: Full gate**

Run: `uv run poe check`
Expected: green (the rewrite must not regress any suite; the live `kc_tagging` eval suite is unaffected — still driven through the same `tag_chunk`). Keeping the existing `kc_tagging` behavioral tests green through the DSPy path **is** the D8 rewrite-no-regression guarantee: the uncompiled program must reproduce the old tag/threshold/dedup behavior before any optimization is applied.

- [ ] **Step 6: Commit**

```bash
git add app/learning/kc_tagging.py tests/test_kc_tagging.py
git commit -m "feat(9c): route kc_tagging through the DSPy program (public API unchanged)"
```

---

### Task 5: Expand the golden kc_tagging dataset (4 → ~24 cases)

**Files:**
- Modify: `tests/eval/cases/kc_tagging.json` (grow to ≥ 24 cases)
- Test: `tests/eval/test_prompts_dataset.py`

**Interfaces:**
- Consumes: `tests/eval/harness.load_kc_tagging_cases` (existing loader).
- Produces: an expanded golden set (each case: `id`, `text`, `candidates: list[str]`, `expect: list[int]` of 1-based indices).

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_prompts_dataset.py`:

```python
"""The expanded golden kc_tagging set is large + well-formed (Phase 9c)."""

from tests.eval.harness import load_kc_tagging_cases


def test_kc_tagging_set_is_large_and_well_formed() -> None:
    cases = load_kc_tagging_cases()
    assert len(cases) >= 24
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))  # unique ids
    for c in cases:
        assert c.text and c.candidates
        assert all(1 <= i <= len(c.candidates) for i in c.expect)  # indices in range
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_prompts_dataset.py -v`
Expected: FAIL — only 4 cases (`assert len(cases) >= 24`).

- [ ] **Step 3: Author the cases**

Grow `tests/eval/cases/kc_tagging.json` to ≥ 24 hand-authored cases across varied subjects, keeping the existing 4. Cover: single-KC hits, multi-KC (2 teaches), zero-KC (passage teaches none of the candidates → `expect: []`), and near-miss/tangential mentions (mentioned but not taught → excluded). Each entry:

```json
{
  "id": "cell-respiration-single",
  "text": "Cellular respiration breaks down glucose in the mitochondria to release ATP, consuming oxygen and producing carbon dioxide and water.",
  "candidates": ["Photosynthesis", "Cellular respiration", "Plate tectonics"],
  "expect": [2]
}
```

Author ~20 more in this shape (short, unambiguous, realistic passages; deterministic). Include several with `"expect": []` and several multi-index.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_prompts_dataset.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/eval/cases/kc_tagging.json tests/eval/test_prompts_dataset.py
git commit -m "feat(9c): expand golden kc_tagging set to 24+ cases for train/dev"
```

---

### Task 6: Trainset split + DSPy metric

**Files:**
- Create: `tests/eval/prompts/trainset.py`, `tests/eval/prompts/metric.py`
- Test: `tests/eval/test_prompts_trainset.py`

**Interfaces:**
- Consumes: `dspy`; `tests.eval.harness.load_kc_tagging_cases`, `KCTaggingCase`.
- Produces:
  - `to_example(case) -> dspy.Example` — maps a golden case to a DSPy example (`passage`, `candidates` catalog string, `expect: list[int]`), with `.with_inputs("passage", "candidates")`.
  - `load_kc_tagging_examples(*, dev_fraction: float = 0.33, seed: int = 0) -> tuple[list[dspy.Example], list[dspy.Example]]` — deterministic train/dev split.
  - `kc_set_match(example, prediction, trace=None) -> bool` (in `metric.py`) — exact set match of predicted candidate numbers vs `example.expect`.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_prompts_trainset.py`:

```python
"""Deterministic train/dev split + exact-set-match metric (Phase 9c)."""

import types

from tests.eval.prompts.metric import kc_set_match
from tests.eval.prompts.trainset import load_kc_tagging_examples


def test_split_is_deterministic_and_disjoint() -> None:
    train_a, dev_a = load_kc_tagging_examples(seed=0)
    train_b, dev_b = load_kc_tagging_examples(seed=0)
    assert [e.passage for e in train_a] == [e.passage for e in train_b]  # deterministic
    assert [e.passage for e in dev_a] == [e.passage for e in dev_b]
    train_ids = {e.passage for e in train_a}
    dev_ids = {e.passage for e in dev_a}
    assert train_ids.isdisjoint(dev_ids)  # no leakage
    assert len(train_a) > 0 and len(dev_a) > 0


def test_metric_is_exact_set_match() -> None:
    example = types.SimpleNamespace(expect=[1, 3])
    good = types.SimpleNamespace(tags=[types.SimpleNamespace(kc=3), types.SimpleNamespace(kc=1)])
    bad = types.SimpleNamespace(tags=[types.SimpleNamespace(kc=1)])
    assert kc_set_match(example, good) is True   # order-insensitive
    assert kc_set_match(example, bad) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_prompts_trainset.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.prompts.trainset'`.

- [ ] **Step 3: Write the implementations**

Create `tests/eval/prompts/metric.py`:

```python
"""Exact-set-match metric for kc_tagging, consistent with harness.score_kc_tagging (Phase 9c)."""

from __future__ import annotations


def kc_set_match(example, prediction, trace=None) -> bool:
    """True iff the predicted candidate numbers exactly match the expected set."""
    try:
        predicted = {int(t.kc) for t in prediction.tags}
    except (AttributeError, TypeError, ValueError):
        return False
    return predicted == set(example.expect)
```

Create `tests/eval/prompts/trainset.py`:

```python
"""Golden kc_tagging cases -> DSPy examples with a deterministic train/dev split (Phase 9c)."""

from __future__ import annotations

import random

import dspy

from tests.eval.harness import KCTaggingCase, load_kc_tagging_cases


def to_example(case: KCTaggingCase) -> dspy.Example:
    catalog = "\n".join(f"{i}. {name}" for i, name in enumerate(case.candidates, start=1))
    return dspy.Example(
        passage=case.text, candidates=catalog, expect=list(case.expect)
    ).with_inputs("passage", "candidates")


def load_kc_tagging_examples(
    *, dev_fraction: float = 0.33, seed: int = 0
) -> tuple[list[dspy.Example], list[dspy.Example]]:
    """Deterministic train/dev split over the golden set (sorted by id, shuffled by seed)."""
    cases = sorted(load_kc_tagging_cases(), key=lambda c: c.id)
    examples = [to_example(c) for c in cases]
    random.Random(seed).shuffle(examples)
    n_dev = max(1, round(len(examples) * dev_fraction))
    return examples[n_dev:], examples[:n_dev]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_prompts_trainset.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/prompts/trainset.py tests/eval/prompts/metric.py tests/eval/test_prompts_trainset.py
git commit -m "feat(9c): kc_tagging DSPy trainset split + exact-set-match metric"
```

---

### Task 7: Compile + report CLIs, poe wiring, final gate

**Files:**
- Create: `tests/eval/prompts/compile.py`, `tests/eval/prompts/report.py`
- Modify: `pyproject.toml` (`[tool.poe.tasks]`: `compile-prompt`, `prompt-report`)
- Test: `tests/eval/test_prompts_report.py`

**Interfaces:**
- Consumes: `dspy`; `load_kc_tagging_examples` + `kc_set_match` (Task 6); `KCTaggingProgram`, `ARTIFACT_PATH`, `load_kc_tagging_program` (Task 3); `RoleLM` (Task 2); `app.core.config.get_settings`, `app.llm.registry.build_llm_client`; the 9a `tests.eval.sweep.tracking.MLflowTracker`.
- Produces: `poe compile-prompt` (compiles + saves the artifact) and `poe prompt-report` (baseline-vs-compiled delta on dev, logged to MLflow); a pure `format_delta(baseline, candidate) -> str` in `report.py` that CI tests directly.

- [ ] **Step 1: Write the failing test (pure delta formatter only — compile/report need a live model)**

Create `tests/eval/test_prompts_report.py`:

```python
"""The prompt-report delta formatter is pure + correct (Phase 9c). Compile/report runs are manual."""

from tests.eval.prompts.report import format_delta


def test_format_delta_reports_signed_change() -> None:
    out = format_delta(baseline=0.60, candidate=0.80)
    assert "baseline" in out and "compiled" in out
    assert "+0.200" in out  # signed improvement


def test_cli_modules_import_cleanly() -> None:
    import tests.eval.prompts.compile  # noqa: F401
    import tests.eval.prompts.report  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_prompts_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.prompts.report'`.

- [ ] **Step 3: Write `compile.py`**

```python
"""`poe compile-prompt kc_tagging` — BootstrapFewShot-compile the kc_tagging program (Phase 9c).

Paid/slow/manual (needs a live model via the FAST role). Saves the optimized program (state-only
JSON) to app/prompts/artifacts/kc_tagging.json — commit it if `poe prompt-report` shows a win.
Not part of `poe check`/`poe eval`.
"""

from __future__ import annotations

import sys

import dspy

from app.core.config import get_settings
from app.llm import ModelRole
from app.llm.registry import build_llm_client
from app.prompts.kc_tagging_program import ARTIFACT_PATH, KCTaggingProgram
from app.prompts.lm import RoleLM
from tests.eval.prompts.metric import kc_set_match
from tests.eval.prompts.trainset import load_kc_tagging_examples


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "kc_tagging":
        print("usage: poe compile-prompt kc_tagging", file=sys.stderr)
        return 2
    train, _dev = load_kc_tagging_examples()
    lm = RoleLM(ModelRole.FAST, build_llm_client(get_settings()))
    with dspy.context(lm=lm):
        optimizer = dspy.BootstrapFewShot(metric=kc_set_match, max_bootstrapped_demos=4)
        compiled = optimizer.compile(KCTaggingProgram(), trainset=train)
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    compiled.save(str(ARTIFACT_PATH), save_program=False)
    print(f"compiled kc_tagging -> {ARTIFACT_PATH} ({len(train)} train examples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Write `report.py`**

```python
"""`poe prompt-report kc_tagging` — measure compiled vs. uncompiled on the held-out dev set (9c).

Paid/manual. Evaluates the uncompiled program (baseline) and the compiled artifact (candidate) on
the dev split with the exact-set-match metric, logs both to MLflow (9a Tracker), prints the delta.
"""

from __future__ import annotations

import sys

import dspy

from app.core.config import get_settings
from app.llm import ModelRole
from app.llm.registry import build_llm_client
from app.prompts.kc_tagging_program import KCTaggingProgram, load_kc_tagging_program
from app.prompts.lm import RoleLM
from tests.eval.prompts.metric import kc_set_match
from tests.eval.prompts.trainset import load_kc_tagging_examples
from tests.eval.sweep.tracking import MLflowTracker


def format_delta(*, baseline: float, candidate: float) -> str:
    return (
        f"kc_tagging exact-match  baseline(uncompiled)={baseline:.3f}  "
        f"compiled={candidate:.3f}  delta={candidate - baseline:+.3f}"
    )


def _score(program, dev, lm) -> float:
    with dspy.context(lm=lm):
        passed = sum(bool(kc_set_match(ex, program(**ex.inputs()))) for ex in dev)
    return passed / len(dev) if dev else 0.0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "kc_tagging":
        print("usage: poe prompt-report kc_tagging", file=sys.stderr)
        return 2
    _train, dev = load_kc_tagging_examples()
    lm = RoleLM(ModelRole.FAST, build_llm_client(get_settings()))
    baseline = _score(KCTaggingProgram(), dev, lm)
    candidate = _score(load_kc_tagging_program(), dev, lm)
    tracker = MLflowTracker(experiment="prompt-kc_tagging")
    for name, score in (("uncompiled", baseline), ("compiled", candidate)):
        tracker.start_run(name)
        tracker.log_metrics({"kc_tagging_exact_match": score})
        tracker.end_run()
    print(format_delta(baseline=baseline, candidate=candidate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: confirm `dspy.Example.inputs()` returns a mapping usable as `program(**ex.inputs())` at the pinned version; if not, pass `passage=ex.passage, candidates=ex.candidates` explicitly.

- [ ] **Step 5: Wire the poe tasks**

In `pyproject.toml` under `[tool.poe.tasks]`, near `build-calibration-dataset`:

```toml
compile-prompt = "python -m tests.eval.prompts.compile"
prompt-report = "python -m tests.eval.prompts.report"
```

- [ ] **Step 6: Run the report test + full gate**

Run: `uv run pytest tests/eval/test_prompts_report.py -v` → PASS.
Run: `uv run poe check` → green (lint + type + whole suite; the paid compile/report are NOT invoked). Confirm `import tests.eval.prompts.compile` / `report` succeed with no live-model call at import time.

- [ ] **Step 7: Commit**

```bash
git add tests/eval/prompts/compile.py tests/eval/prompts/report.py tests/eval/test_prompts_report.py pyproject.toml
git commit -m "feat(9c): compile-prompt + prompt-report CLIs (BootstrapFewShot + MLflow delta)"
```

---

### Manual verification (paid — needs a live FAST model; not CI)

Do this once to produce + commit the artifact and prove the delta:

- [ ] With a FAST model available (Ollama up, or an OpenRouter key set), run `uv run poe compile-prompt kc_tagging`. Expected: writes `app/prompts/artifacts/kc_tagging.json`.
- [ ] Run `uv run poe prompt-report kc_tagging`. Expected: prints `baseline(uncompiled)=… compiled=… delta=±…` and logs two MLflow runs. A non-negative delta means optimization helped on held-out dev.
- [ ] If the delta is a win, commit the artifact: `git add app/prompts/artifacts/kc_tagging.json && git commit -m "chore(9c): commit compiled kc_tagging artifact"`. If not, leave it uncommitted (runtime keeps using the uncompiled fallback) and note the finding.
- [ ] Sanity: with the artifact committed, `uv run poe check` still green (the committed artifact loads via `load_kc_tagging_program` on the fake client without error).

---

## Notes for the implementer

- **Verify DSPy's API at the pinned version FIRST (Task 1).** The custom-LM contract is mid-migration and typed-output/adapter formats vary. Task 1 pins the exact response shape and the fake-reply format; reuse them verbatim in Tasks 2–4's tests. Don't guess — run the probe.
- **Async decision propagates from Task 1.** If `program.acall()`→`aforward` is clean, use the async path in Task 4 (primary). If not, use the threadpool bridge (Approach B) — both are written out in Task 4; pick one based on Task 1's finding and delete the other.
- **`app/prompts/` is a regular package** (`__init__.py` present); **`tests/eval/prompts/` is a namespace package** (no `__init__.py`). Don't mix these up.
- **Never let `tag_chunk` raise.** The `except Exception` best-effort wrapper in Task 4 is load-bearing — a weak model or a parse failure must yield `[]`, exactly as the pre-9c behavior did.
- **DSPy only in `app/prompts/`** (and the offline `tests/eval/prompts/`). `app/learning/kc_tagging.py` may `import dspy` only for the `dspy.context(...)` call in Task 4; if a reviewer prefers, wrap that context management inside `app/prompts/kc_tagging_program.py` (e.g. a `run_tagging(program, lm, passage, candidates)` helper) to keep `dspy` out of `app/learning/` entirely — acceptable either way.
- **Offline compile/report drive the async client via `RoleLM.forward` → `asyncio.run` per call (Task 7).** If the pinned provider holds a persistent, loop-bound `httpx.AsyncClient`, repeated `asyncio.run` can error (`Event loop is closed` / client bound to another loop). Before committing the artifact, **run a real multi-example compile** to confirm it works. If it errors, either (a) confirm/adjust providers to open a fresh client per `complete` call, or (b) keep one persistent loop for the whole run — wrap the sync `optimizer.compile`/report body in `await asyncio.to_thread(...)` under a single `asyncio.run` in `main`, and have `RoleLM.forward` bridge back to that loop via `run_coroutine_threadsafe` (the same Approach-B mechanism as Task 4's runtime fallback). This only affects the offline CLIs, never the async runtime path.
