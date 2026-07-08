# LangGraph Orchestration Substrate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce LangGraph and migrate the existing tutor turn onto a single-node state graph, with byte-identical SSE behavior.

**Architecture:** LangGraph is the state-machine runtime only. A `generate` node (a plain async function) streams tokens by calling our existing role-based `LLMClient` — no LangChain chat models. The graph lives in `app/agent/`; a thin persistence orchestrator (`run_tutor_turn`) in `app/services/chat.py` drives the graph and owns DB writes + commit boundaries, so the non-serializable `AsyncSession` never enters graph state. The router shrinks to an SSE mapper.

**Tech Stack:** Python 3.13, LangGraph, FastAPI + SSE, SQLAlchemy 2.0 async, our `app/llm` role registry, pytest + `FakeProvider` (offline).

## Global Constraints

- **LLM by role only.** Nodes call `llm.stream(ModelRole.SMART, …)` / `llm.spec(ModelRole.SMART)`; never a provider SDK or a hardcoded model name.
- **Byte-identical SSE.** The wire JSON stays exactly: `{"type":"token","text":…}`, `{"type":"error","detail":"generation failed"}`, `{"type":"done","message_id":…,"usage":{"input_tokens":…,"output_tokens":…},"cost_usd":…}`. `tests/test_chat.py` must pass **unmodified**.
- **Graph state is checkpoint-serializable.** `TutorState` holds only `ChatMessage`/`Usage`/primitives — no `AsyncSession`, no ORM rows, no `LLMClient`.
- **Layering:** `api → services → (llm | agent) → models/db`. `app/agent` may import `app/llm` + `app/models` but **not** `app/services`. `run_tutor_turn` lives in `app/services/chat.py` (services → agent).
- **Persist boundaries unchanged.** User message persisted + committed *before* streaming; assistant message + `LLMCall` persisted + committed *after* a clean stream; on stream failure, no assistant row.
- **Commit convention:** message `Phase 5 (N/n): …`, ending with the trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Run `uv run poe format` before each commit (pre-commit's ruff-format will otherwise abort the commit).
- **Green gate:** `uv run poe check` (lint + type-check + test) passes at each task boundary.

---

### Task 1: LangGraph substrate — `TutorState` + graph + `generate` node

**Files:**
- Modify: `pyproject.toml` (add `langgraph` dependency) + `uv.lock`
- Create: `app/agent/__init__.py`
- Create: `app/agent/state.py`
- Create: `app/agent/tutor.py`
- Test: `tests/test_agent_tutor.py`

**Interfaces:**
- Consumes: `app.llm.registry.fake_llm_client`, `app.llm.registry.LLMClient`; `app.llm.types.{ChatMessage, ChatRole, ModelRole, Usage}`.
- Produces:
  - `TutorState` (TypedDict): keys `messages: list[ChatMessage]`, `system: str`, `max_tokens: int`, `reply: str`, `usage: Usage`.
  - `build_tutor_graph(llm: LLMClient) -> CompiledStateGraph` — compiled `START → generate → END`, no checkpointer. The `generate` node streams tokens via `get_stream_writer()` (payloads `{"token": <str>}`) and returns `{"reply": <str>, "usage": Usage}`.

- [ ] **Step 1: Add the dependency**

Run: `uv add langgraph`
Then verify the runtime imports resolve:
Run: `uv run python -c "from langgraph.graph import StateGraph, START, END; from langgraph.config import get_stream_writer; from langgraph.graph.state import CompiledStateGraph; print('ok')"`
Expected: prints `ok`. (`uv add` appends `langgraph>=<resolved>` to `pyproject.toml` dependencies and updates `uv.lock`.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_agent_tutor.py`:

```python
"""LangGraph tutor substrate: the graph + node stream and fill state via FakeProvider."""

from app.agent.tutor import TutorState, build_tutor_graph
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole, Usage

REPLY = "Let us explore this together."


def _state(user: str = "What is a derivative?") -> TutorState:
    return {
        "messages": [ChatMessage(role=ChatRole.USER, content=user)],
        "system": "You are a tutor.",
        "max_tokens": 256,
        "reply": "",
        "usage": Usage(),
    }


async def test_generate_node_fills_reply_and_usage() -> None:
    graph = build_tutor_graph(fake_llm_client(REPLY))
    final = await graph.ainvoke(_state())
    assert final["reply"] == REPLY
    assert final["usage"].output_tokens == len(REPLY.split())


async def test_graph_streams_tokens_incrementally() -> None:
    graph = build_tutor_graph(fake_llm_client(REPLY))
    tokens: list[str] = []
    async for mode, payload in graph.astream(_state(), stream_mode=["custom", "values"]):
        if mode == "custom":
            tokens.append(payload["token"])
    # One custom payload per streamed word chunk (proves incremental, not buffered).
    assert len(tokens) == len(REPLY.split())
    assert "".join(tokens) == REPLY
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_tutor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent'` (module not created yet).

- [ ] **Step 4: Create the state schema**

Create `app/agent/state.py`:

```python
"""LangGraph state for the tutor turn.

Checkpoint-serializable by construction — only Pydantic/primitive fields, never an
AsyncSession, ORM row, or LLMClient. Grows (goal, pending_question, kc_ids) in later slices.
"""

from typing import TypedDict

from app.llm.types import ChatMessage, Usage


class TutorState(TypedDict):
    messages: list[ChatMessage]  # full context incl. the new user turn
    system: str
    max_tokens: int
    reply: str  # filled by the generate node
    usage: Usage  # filled by the generate node
```

- [ ] **Step 5: Create the graph + node**

Create `app/agent/tutor.py`:

```python
"""The tutor turn as a single-node LangGraph state graph.

`build_tutor_graph(llm)` closes over the request's LLM client (kept out of state, which
must stay serializable) and compiles START -> generate -> END with no checkpointer (this
turn is not HITL). The generate node streams tokens over the custom stream writer while
accumulating the full reply + final usage.
"""

from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.state import TutorState
from app.llm.registry import LLMClient
from app.llm.types import ModelRole, Usage

__all__ = ["TutorState", "build_tutor_graph"]


def build_tutor_graph(llm: LLMClient) -> CompiledStateGraph:
    async def generate(state: TutorState) -> dict[str, Any]:
        writer = get_stream_writer()
        parts: list[str] = []
        usage = Usage()
        async for chunk in llm.stream(
            ModelRole.SMART,
            state["messages"],
            system=state["system"],
            max_tokens=state["max_tokens"],
        ):
            if chunk.text:
                parts.append(chunk.text)
                writer({"token": chunk.text})
            if chunk.usage is not None:
                usage = chunk.usage
        return {"reply": "".join(parts), "usage": usage}

    graph = StateGraph(TutorState)
    graph.add_node("generate", generate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    return graph.compile()
```

Create `app/agent/__init__.py`:

```python
"""Agent layer: LangGraph orchestration graphs (nodes pull models by role)."""

from app.agent.state import TutorState
from app.agent.tutor import build_tutor_graph

__all__ = ["TutorState", "build_tutor_graph"]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_tutor.py -v`
Expected: PASS (both tests).
> If `get_stream_writer()` raises under `ainvoke` on the installed version, switch `test_generate_node_fills_reply_and_usage` to drive `graph.astream(_state(), stream_mode=["values"])` and read the final `values` payload — the writer is active in any `astream`/`ainvoke` run, so this is only a version-contingency note.

- [ ] **Step 7: Green gate + commit**

Run: `uv run poe format && uv run poe check`
Expected: lint + type-check + all tests pass.

```bash
git add pyproject.toml uv.lock app/agent/ tests/test_agent_tutor.py
git commit -m "$(cat <<'EOF'
Phase 5 (2/n): LangGraph substrate — TutorState + single-node tutor graph

Introduce langgraph; app/agent/ holds a checkpoint-serializable TutorState and
build_tutor_graph(llm), a START->generate->END graph whose node streams tokens
via the custom stream writer by calling our role-based LLMClient (no LangChain
models). Tested offline through FakeProvider.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `run_tutor_turn` orchestrator + `TurnEvent` (persistence + streaming)

**Files:**
- Modify: `app/services/chat.py` (add `TurnEvent`, `run_tutor_turn`, a module logger)
- Test: `tests/test_agent_tutor.py` (add DB-backed cases)

**Interfaces:**
- Consumes: `app.agent.tutor.{TutorState, build_tutor_graph}`; existing `app.services.chat.{TUTOR_SYSTEM_PROMPT, to_chat_messages, add_message, record_llm_call}`; `app.llm.pricing.cost_usd`; `app.llm.registry.LLMClient`; `app.llm.types.{ChatMessage, ChatRole, ModelRole, Usage}`; `app.models.chat.Message`.
- Produces:
  - `TurnEvent` (frozen dataclass): `type: Literal["token","error","done"]`, `text: str = ""`, `detail: str = ""`, `message_id: str | None = None`, `usage: Usage = Usage()`, `cost_usd: float = 0.0`.
  - `run_tutor_turn(session, llm, *, learner_id: uuid.UUID, conversation_id: uuid.UUID, history: Sequence[Message], user_content: str, max_tokens: int) -> AsyncIterator[TurnEvent]` — persists the user turn (+commit), streams `token` events, then persists the assistant `Message` + `LLMCall` (+commit) and yields a terminal `done`; on stream failure yields `error` and persists no assistant row.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_tutor.py`:

```python
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatChunk, ModelRole
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.services.chat import TurnEvent, run_tutor_turn


class _BoomProvider(FakeProvider):
    """A provider whose stream raises immediately (simulates mid-generation failure)."""

    async def stream(self, *, model, messages, system=None, max_tokens=1024):
        raise RuntimeError("boom")
        yield ChatChunk()  # unreachable; makes this an async generator


async def _conversation(session: AsyncSession) -> Conversation:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def _drain(session: AsyncSession, llm: LLMClient, conv: Conversation) -> list[TurnEvent]:
    return [
        ev
        async for ev in run_tutor_turn(
            session,
            llm,
            learner_id=conv.learner_id,
            conversation_id=conv.id,
            history=[],
            user_content="What is a derivative?",
            max_tokens=256,
        )
    ]


async def test_run_tutor_turn_persists_turn_and_logs_call(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    events = await _drain(db_session, fake_llm_client(REPLY), conv)

    assert [e.type for e in events if e.type != "token"][-1] == "done"
    assert "".join(e.text for e in events if e.type == "token") == REPLY
    done = next(e for e in events if e.type == "done")
    assert done.message_id is not None
    assert done.usage.output_tokens == len(REPLY.split())

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == REPLY
    assert messages[1].model == "fake-1"

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "smart"
    assert calls[0].output_tokens == len(REPLY.split())


async def test_run_tutor_turn_stream_failure_persists_user_only(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    client = LLMClient({"fake": _BoomProvider()}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})

    events = await _drain(db_session, client, conv)

    assert events[-1].type == "error"
    assert events[-1].detail == "generation failed"
    messages = (
        await db_session.scalars(select(Message).where(Message.conversation_id == conv.id))
    ).all()
    assert [m.role for m in messages] == ["user"]  # user persisted, no assistant
    assert (await db_session.scalars(select(LLMCall))).all() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_tutor.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_tutor_turn' from 'app.services.chat'`.

- [ ] **Step 3: Implement `TurnEvent` + `run_tutor_turn`**

Edit `app/services/chat.py`. Add these imports near the top (merge with the existing import block; `ModelRole` joins the existing `app.llm.types` import):

```python
from collections.abc import AsyncIterator, Sequence  # Sequence already imported; add AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

import structlog

from app.agent.tutor import TutorState, build_tutor_graph
from app.llm.pricing import cost_usd
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage  # add ModelRole

log = structlog.get_logger(__name__)
```

Append to `app/services/chat.py`:

```python
@dataclass(frozen=True)
class TurnEvent:
    """A streamed step of a tutor turn. The router maps these to SSE frames."""

    type: Literal["token", "error", "done"]
    text: str = ""
    detail: str = ""
    message_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0


async def run_tutor_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    history: Sequence[Message],
    user_content: str,
    max_tokens: int,
) -> AsyncIterator[TurnEvent]:
    """Persist the user turn, stream the tutor's reply through the graph, then persist it."""
    messages = to_chat_messages(history)
    messages.append(ChatMessage(role=ChatRole.USER, content=user_content))
    await add_message(session, conversation_id, ChatRole.USER.value, user_content)
    await session.commit()

    spec = llm.spec(ModelRole.SMART)
    initial: TutorState = {
        "messages": messages,
        "system": TUTOR_SYSTEM_PROMPT,
        "max_tokens": max_tokens,
        "reply": "",
        "usage": Usage(),
    }

    reply = ""
    usage = Usage()
    try:
        async for mode, payload in build_tutor_graph(llm).astream(
            initial, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield TurnEvent(type="token", text=payload["token"])
            elif mode == "values":
                reply = payload["reply"]
                usage = payload["usage"]
    except Exception as exc:
        log.error("tutor.stream_failed", error=str(exc), model=spec.model)
        yield TurnEvent(type="error", detail="generation failed")
        return

    assistant = await add_message(
        session, conversation_id, ChatRole.ASSISTANT.value, reply, model=spec.model
    )
    cost = cost_usd(spec.model, usage)
    await record_llm_call(
        session,
        learner_id=learner_id,
        conversation_id=conversation_id,
        role=ModelRole.SMART.value,
        provider=spec.provider,
        model=spec.model,
        usage=usage,
        cost_usd=cost,
    )
    await session.commit()
    log.info(
        "llm.call",
        role=ModelRole.SMART.value,
        provider=spec.provider,
        model=spec.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )
    yield TurnEvent(
        type="done", message_id=str(assistant.id), usage=usage, cost_usd=cost
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_tutor.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Green gate + commit**

Run: `uv run poe format && uv run poe check`
Expected: green.

```bash
git add app/services/chat.py tests/test_agent_tutor.py
git commit -m "$(cat <<'EOF'
Phase 5 (3/n): run_tutor_turn — persistence orchestrator around the graph

A chat-service wrapper drives build_tutor_graph via astream(["custom","values"]),
yielding token/error/done TurnEvents. Owns commit boundaries (user turn before
streaming; assistant Message + LLMCall after a clean stream; no assistant row on
failure), keeping the AsyncSession out of graph state. Layer-correct home
(services -> agent). DB-backed tests via FakeProvider.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Migrate the router to `run_tutor_turn` (byte-identical SSE)

**Files:**
- Modify: `app/api/v1/chat.py` (`send_message` + import cleanup)
- Test: `tests/test_chat.py` (unchanged — the parity oracle)

**Interfaces:**
- Consumes: `app.services.chat.run_tutor_turn`, `app.services.chat.get_conversation`, `app.services.chat.list_messages`; `app.core.config.get_settings`.
- Produces: no new symbols — `send_message` becomes a thin SSE mapper over `TurnEvent`s.

- [ ] **Step 1: Run the existing parity test (baseline, still green pre-change)**

Run: `uv run pytest tests/test_chat.py -v`
Expected: PASS (3 tests) — this is the behavior we must preserve.

- [ ] **Step 2: Replace `send_message` and clean imports**

In `app/api/v1/chat.py`, replace the `send_message` function (currently lines ~52-131) with:

```python
@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    data: ChatTurnRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
) -> StreamingResponse:
    """Persist the user turn, then stream the tutor's reply as Server-Sent Events."""
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")

    history = await svc.list_messages(session, conversation_id)
    max_tokens = get_settings().chat_max_tokens

    async def event_stream() -> AsyncIterator[str]:
        async for ev in svc.run_tutor_turn(
            session,
            llm,
            learner_id=learner.id,
            conversation_id=conversation_id,
            history=history,
            user_content=data.content,
            max_tokens=max_tokens,
        ):
            if ev.type == "token":
                yield _sse({"type": "token", "text": ev.text})
            elif ev.type == "error":
                yield _sse({"type": "error", "detail": ev.detail})
            elif ev.type == "done":
                yield _sse(
                    {
                        "type": "done",
                        "message_id": ev.message_id,
                        "usage": {
                            "input_tokens": ev.usage.input_tokens,
                            "output_tokens": ev.usage.output_tokens,
                        },
                        "cost_usd": ev.cost_usd,
                    }
                )

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Then remove now-unused imports from the top of `app/api/v1/chat.py`: `structlog` and the `log = structlog.get_logger(__name__)` line; `from app.llm.pricing import cost_usd`; and narrow the `app.llm.types` import to only what remains used (`ModelRole`, `ChatMessage`, `ChatRole`, `Usage` are no longer referenced here — delete that import line entirely). Keep: `json`, `uuid`, `AsyncIterator`, `Any`, `APIRouter/HTTPException/status`, `StreamingResponse`, the `app.api.deps` names, `get_settings`, the `app.schemas.chat` names, and `from app.services import chat as svc`.

- [ ] **Step 3: Run the parity test to verify it still passes**

Run: `uv run pytest tests/test_chat.py -v`
Expected: PASS (all 3 tests, unmodified) — SSE frames byte-identical.

- [ ] **Step 4: Green gate + commit**

Run: `uv run poe format && uv run poe check`
Expected: green (ruff flags any leftover unused import — fix before committing).

```bash
git add app/api/v1/chat.py
git commit -m "$(cat <<'EOF'
Phase 5 (4/n): route the tutor turn through the LangGraph orchestrator

send_message shrinks to an SSE mapper over run_tutor_turn's TurnEvents; the wire
format is unchanged and tests/test_chat.py passes unmodified. Turn generation +
persistence now live behind the graph.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Documentation — reflect the confirmed orchestration shape

**Files:**
- Modify: `docs/TECHNICAL_DESIGN.md` (§3.3 and §4)
- Modify: `docs/ROADMAP.md` (Phase 5 progress note)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update TECHNICAL_DESIGN §3.3**

In `docs/TECHNICAL_DESIGN.md`, replace the §3.3 paragraph that says the registry "returns a **LangChain-compatible chat model** (or a thin adapter…)" with the confirmed reality:

```markdown
### 3.3 LangGraph compatibility & cost

LangGraph nodes consume models **by role through our existing `LLMClient`** — our client
*is* the adapter; we do not introduce LangChain chat models. Nodes are plain async functions
that call `llm.stream(ModelRole.SMART, …)` / `llm.complete(…)`, so graphs stay
provider-agnostic and there is a single model abstraction. Token streaming is surfaced with
LangGraph's **custom stream writer** (`get_stream_writer()`; `stream_mode="custom"`), not
`stream_mode="messages"`. Every call records `usage` + computed cost, tagged by `(role,
model, request_id, graph_node)`, into a `llm_calls` log → cost dashboards.
```

- [ ] **Step 2: Update TECHNICAL_DESIGN §4**

In `docs/TECHNICAL_DESIGN.md` §4, under the tutoring-turn description, add a note that persistence is orchestrated outside the graph:

```markdown
**State & persistence:** graph state must be checkpoint-serializable (for HITL interrupts),
so the `AsyncSession` never lives in state — turn persistence (user/assistant messages,
`LLMCall`) is orchestrated in a thin service wrapper (`run_tutor_turn`) around the graph.
Nodes that need the session later (e.g. tracer-update) receive it via LangGraph runtime
context, not state.
```

- [ ] **Step 3: Update ROADMAP Phase 5 progress note**

In `docs/ROADMAP.md`, under Phase 5, append a progress note beneath the scope list (do **not** check the first bullet — it also covers lesson-generation, untouched here):

```markdown
> **Substrate landed.** LangGraph introduced behind `app/agent/`; the tutor turn now runs as
> a single-node state graph (`build_tutor_graph`) driven by a `run_tutor_turn` service that
> owns persistence, with byte-identical SSE. Nodes call our role-based `LLMClient` (no
> LangChain models); token streaming rides the custom stream writer. Refinement gate,
> retrieve/tracer nodes, and lesson-generation graphs are the next Phase 5 slices.
```

- [ ] **Step 4: Verify end-to-end + commit**

Drive the real SSE turn once to observe parity (not just asserted) — with the offline fake, and against a live Ollama `SMART` if reachable:

Run: `uv run pytest tests/test_chat.py tests/test_agent_tutor.py -v`
Expected: PASS.

Optionally, with the dev server up (`uv run poe dev`) and a conversation created, `curl -N` the messages endpoint and confirm the `token…done` frame sequence matches pre-migration output.

Run: `uv run poe check`
Expected: green.

```bash
git add docs/TECHNICAL_DESIGN.md docs/ROADMAP.md
git commit -m "$(cat <<'EOF'
Phase 5 (5/n): docs — confirmed LangGraph orchestration shape

TECHNICAL_DESIGN §3.3 (our LLMClient is the adapter; custom stream writer, no
LangChain models) + §4 (session stays out of serializable graph state; wrapper
owns persistence). ROADMAP Phase 5 substrate-landed note.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- Thinnest lift-and-shift (only the tutor turn, one generate node) → Tasks 1–3. ✅
- Nodes call our `LLMClient`, no LangChain models; custom stream writer → Task 1 node + Task 4 §3.3. ✅
- No orchestration Protocol seam; graph tested via `FakeProvider` → Tasks 1–2 tests. ✅
- Persistence in the wrapper; session out of serializable state → Task 2 `run_tutor_turn` + `TutorState` (Task 1). ✅
- Byte-identical SSE; `tests/test_chat.py` unmodified → Task 3. ✅
- `TutorState` schema; `app/agent/` layout → Task 1. (Refinement: `run_tutor_turn`/`TurnEvent` live in `app/services/chat.py` not `app/agent/`, for layer-correctness — noted in Global Constraints.) ✅
- Testing: graph unit, streaming contract, DB e2e, failure path, parity → Tasks 1–3. ✅
- `langgraph` dependency added → Task 1 Step 1. ✅
- Doc updates (§3.3, §4, ROADMAP) → Task 4. ✅
- Verification (`poe check` + drive the SSE turn) → Task 4 Step 4 + per-task gates. ✅

**Placeholder scan:** no TBD/TODO/"handle edge cases"; every code step shows complete code. ✅

**Type consistency:** `TutorState` keys (`messages/system/max_tokens/reply/usage`) identical across state.py, tutor.py, and run_tutor_turn's `initial`. `build_tutor_graph(llm)` signature consistent (Task 1 produces → Task 2 consumes). `TurnEvent` fields (`type/text/detail/message_id/usage/cost_usd`) identical across Task 2 (produce) and Task 3 (consume). `run_tutor_turn` keyword params identical across Task 2 def and Task 3 call site. ✅
