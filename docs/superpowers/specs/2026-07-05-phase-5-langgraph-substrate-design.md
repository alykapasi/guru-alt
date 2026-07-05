# Phase 5 · Slice 1 — LangGraph orchestration substrate

> **Status:** Approved design. Phase 5's first slice. See
> [ROADMAP.md](../../ROADMAP.md#phase-5) (order) and [TECHNICAL_DESIGN.md](../../TECHNICAL_DESIGN.md)
> §4 (orchestration) for the surrounding context.

## Goal

Introduce **LangGraph** as the orchestration runtime by rebuilding the existing tutor turn as a
single-node **state graph**, with **byte-identical external behavior** — the SSE `token` / `error` /
`done` frames stay exactly as they are today. This is a *refactor of working code*, deliberately the
thinnest foundational slice: it lands the `app/agent/` module, a checkpoint-serializable state
schema, the node pattern, the custom-stream token path, and the test approach that the refinement
gate, lesson-plan policy, and session runner build on next.

Non-goal: adding pedagogy (retrieval grounding, tracer updates, the refinement gate). Those are
separate, later Phase 5 slices (see [Deferred](#deferred-later-phase-5-slices)).

## Decisions (locked)

1. **Scope — thinnest lift-and-shift.** Migrate *only* the existing tutor turn onto a graph with a
   single `generate` node. Same behavior, same SSE output.
2. **LLM integration — LangGraph orchestrates; nodes call our `LLMClient`.** No LangChain chat
   models. Nodes are plain async functions that call the existing role-based client
   (`llm.stream(ModelRole.SMART, …)`). One model abstraction; cost logging stays where it is. Token
   streaming rides a **custom stream writer**, not `stream_mode="messages"`. This revises the loose
   hint in TECHNICAL_DESIGN §3.3 — *our `LLMClient` is the adapter*.
3. **No orchestration Protocol seam.** The swap point we care about is the *model*, already seamed
   via `LLMClient` / `FakeProvider`. The **graph is the unit under test**: build it, invoke it with a
   fake client, assert on state + streamed tokens — exactly how the tracer/grading are tested. YAGNI:
   we are not going to swap orchestration engines, so no `TutorGraph` Protocol + Fake.

## Architecture & boundaries

LangGraph is *only* the state-machine runtime. The load-bearing boundary: **the graph does the
*thinking*; DB persistence stays in a thin service wrapper around it.**

Rationale — LangGraph state must be **checkpoint-serializable** for the HITL refinement gate later,
and an `AsyncSession` is not serializable, so the session must *never* live in graph state. This also
mirrors the RAG pipeline's discipline (network/compute work is orchestrated; DB writes stay
serialized in the caller). When the tracer-update node arrives in a later slice, it receives the
session via LangGraph's **runtime context**, not via state.

```
router (thin SSE mapper)  — app/api/v1/chat.py
   └─ run_tutor_turn(session, llm, …)               — app/agent/, owns persistence + commit boundaries
        ├─ persist user message  (+ commit)          # before streaming, as today
        ├─ graph.astream(state, stream_mode=["custom","values"])   — LangGraph
        │     └─ generate node → streams tokens via writer, returns {reply, usage}
        └─ persist assistant message + LLMCall  (+ commit)
```

## Module layout & state

New package `app/agent/`:

- **`state.py`** — `TutorState`, a LangGraph `TypedDict` schema. Minimal and serializable:
  - inputs: `messages: list[ChatMessage]`, `system: str`, `max_tokens: int`
  - filled by the node: `reply: str`, `usage: Usage`
  - **No** session, ORM objects, or learner rows. Identifiers stay in the wrapper for this slice; the
    schema grows `goal` / `pending_question` / `kc_ids` in later slices.
- **`tutor.py`**
  - `build_tutor_graph() -> CompiledGraph` — one `generate` node, `START → generate → END`, compiled
    **without** a persistent checkpointer (this turn is not HITL).
  - the `generate` node (see below).
  - `run_tutor_turn(session, llm, *, learner_id, conversation_id, history, user_content, max_tokens)
    -> AsyncIterator[TurnEvent]` — the router-facing service that owns persistence.
- **`__init__.py`** — exports `run_tutor_turn`, `TurnEvent`.

`TurnEvent` is a tiny internal event type — `token(text)` / `error(detail)` /
`done(message_id, usage, cost_usd)` — so the router never re-implements turn logic; it just maps
events to SSE frames. (Illustrative shape; the plan fixes final signatures.)

## Streaming & persistence mechanics

- The **`generate` node** iterates `llm.stream(ModelRole.SMART, messages, system=…, max_tokens=…)`.
  For each text chunk it calls LangGraph's `get_stream_writer()` → `writer({"token": text})`, while
  accumulating the full reply and the final `usage`. It returns `{"reply": full, "usage": usage}` as
  its state update.
- **`run_tutor_turn`** drives `graph.astream(state, stream_mode=["custom", "values"])`:
  - `custom` payloads → `TurnEvent.token`
  - the final `values` payload → the completed `reply` / `usage`
  - on any exception during the stream → yield `TurnEvent.error("generation failed")` and return
    **without** persisting an assistant message (exact current behavior).
- On a clean stream: persist the assistant `Message` (with `spec.model`), compute
  `cost_usd(spec.model, usage)`, write the `LLMCall`, commit, then yield `TurnEvent.done`. The user
  message is persisted and committed *before* streaming begins, as today.
- **Router parity.** `send_message` in [app/api/v1/chat.py](../../../app/api/v1/chat.py) shrinks to:
  resolve/authorize the conversation (404 if missing or not owned), load history, then
  `async for ev in run_tutor_turn(...)` → `_sse(...)`. The wire JSON (`type`, `text`, `message_id`,
  `usage.{input,output}_tokens`, `cost_usd`, `detail`) is unchanged — **no client change**, and
  [tests/test_chat.py](../../../tests/test_chat.py) passes **unmodified**.

## Testing

New `tests/test_agent_tutor.py`, all offline via `fake_llm_client(...)`:

- **Graph unit (no DB):** build the graph, invoke with a fake client → the `generate` node fills
  `reply` == the fake reply and captures non-zero `usage`. Proves the node + state contract in
  isolation.
- **Streaming contract (no DB):** drive `run_tutor_turn` with a fake client, collect `token` events →
  `"".join(tokens)` == the fake reply (proves *incremental* custom-stream surfacing, not buffered),
  and exactly one terminal `done` event carrying `usage` + `cost_usd`.
- **DB e2e (`db_session`):** run a turn against a real `Conversation` → the user + assistant `Message`
  rows and the `LLMCall` row persist with the right role / model / cost; a mid-stream failure
  persists the user turn but no assistant message.
- **Parity:** [tests/test_chat.py](../../../tests/test_chat.py) runs unmodified and green.

## Dependency

- Add `langgraph` to `pyproject.toml` main deps. Excluded from the beartype claw like other
  third-party libraries (TECHNICAL_DESIGN §1 convention). Per §11 timing, Phase 5 is exactly when
  LangGraph earns its place (first branching / HITL / multi-step workflows).

## Doc updates (part of this slice)

- **TECHNICAL_DESIGN §3.3** — replace the "LangChain-compatible chat model" lean with the confirmed
  reality: LangGraph nodes call our `LLMClient` by role (our client *is* the adapter); token
  streaming rides a custom stream writer, not `stream_mode="messages"`.
- **TECHNICAL_DESIGN §4** — note that turn persistence is orchestrated in the service wrapper (the
  session is non-serializable, kept out of graph state); the tracer-update node will take the session
  via runtime context.
- **ROADMAP Phase 5** — add a progress note that the LangGraph substrate + tutor-turn migration
  landed. **Do not check the first bullet yet** — it also covers lesson-generation, which this slice
  does not touch.

## Verification

- `uv run poe check` green (lint + type-check + test).
- Actually drive the SSE turn end-to-end — with a fake client, and with a real Ollama `SMART` model
  if reachable — to confirm tokens stream *incrementally* and the `done` / cost frame matches the
  pre-migration output. Behavior parity observed, not just asserted.

## Deferred (later Phase 5 slices)

YAGNI — explicitly out of scope for this slice:

- retrieve-context / RAG-grounding node
- extract-observations → tracer-update node
- the refinement-gate subgraph + a persistent checkpointer (Postgres / SQLite) for HITL
- lesson-generation / grading / content-assembly graphs
- the per-learner memory module
