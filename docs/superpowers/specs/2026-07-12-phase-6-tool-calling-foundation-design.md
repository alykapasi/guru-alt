# Phase 6 · Slice 1 — Tool-calling foundation, retrieval tool, minimal agentic graph

> **Status:** Approved design. Phase 6's first slice. See
> [ROADMAP.md](../../ROADMAP.md#phase-6) (order) and [TECHNICAL_DESIGN.md](../../TECHNICAL_DESIGN.md)
> §4 (orchestration) for the surrounding context.

## Goal

Introduce provider-agnostic **tool calling** into `app/llm/` and build the first tool-using
LangGraph: a bounded agentic loop with one real tool (retrieval-as-tool over the learner's
materials), reachable via a new `mode="agentic"` on the existing chat-turn endpoint. This is
greenfield — `app/llm/` has zero tool-call vocabulary today (confirmed by direct grep: `ChatRole`
is SYSTEM/USER/ASSISTANT only, `ContentPart` is TextPart/ImagePart only, `LLMProvider` has no
`tools` param). Phase 2's roadmap checkbox loosely implying a "tool schema" was never actually
built.

Non-goal: the live/external-data tool, the first structured workflow graph, and any refactor of
the duplicated `astream`-mode-dispatch boilerplate across turn services. Those are separate, later
Phase 6 slices (see [Deferred](#deferred-later-phase-6-slices)).

## Decisions (locked)

1. **Hand-rolled provider-agnostic loop, not LangGraph's prebuilt `create_react_agent`/`ToolNode`,
   not Anthropic's Tool Runner.** Both require a LangChain `BaseChatModel`-shaped object or direct
   Anthropic-SDK usage (confirmed via `inspect.signature(create_react_agent)`: `model` is typed
   `str | Runnable | BaseChatModel | Callable[..., BaseChatModel]`), which conflicts with the
   Phase 5 substrate decision: **"Nodes call our role-based `LLMClient`, no LangChain models"**
   ([ROADMAP.md:187](../../ROADMAP.md), [TECHNICAL_DESIGN.md](../../TECHNICAL_DESIGN.md) §3.3).
   Adopting either would mean writing a `BaseChatModel` shim purely to reintroduce that coupling,
   without even saving the provider-translation work — it would just relocate to a
   LangChain-message adapter.
2. **One canonical tool-call shape in `app/llm/types.py`; each provider translates to/from its own
   wire shape.** Mirrors how `ContentPart = TextPart | ImagePart` already abstracts multimodal
   content differences across providers.
3. **`ChatRole.TOOL` is a real enum member**, not tool results piggybacked onto `USER` content —
   matches this codebase's habit of explicit enums over implicit type-sniffing (`MemoryKind`,
   `ItemType`, etc.).
4. **Only one real tool this slice: `search_materials`**, wrapping the existing
   `app/rag/retrieval.py::retrieve`. The model controls only `query`; `session`/`llm`/`learner_id`/
   `subject_id` bind server-side via closure — there's no "list subjects" tool yet, so a
   model-fillable `subject_id` would only invite hallucinated UUIDs.
5. **No heavier `ToolSpec`/`ToolContext` registry shape.** `build_tools()` returning a plain
   `list[Tool]` is enough for one tool with zero second caller — YAGNI. Promote when the
   live/external-data tool (next slice) adds a real second caller.
6. **`mode` is a per-turn `ChatTurnRequest` field, not a persisted `Conversation.mode`.** No
   migration, strictly more flexible (a learner can have one tool-using turn inside an otherwise
   plain conversation); a persisted default is a trivial follow-up if tier-gating ever wants it.
7. **Iteration-cap-exceeded degrades gracefully**, matching the existing pattern of the refinement
   gate auto-committing at `max_rounds` rather than erroring — persist whatever reply text exists,
   surface `detail="capped"` on the `done` event, log a warning.
8. **Intra-loop tool-call/tool-result exchanges are not persisted as `Message` rows this slice** —
   only the user message and final assistant reply are, exactly like `run_tutor_turn` today. A
   `ToolCall` audit table mirroring `LLMCall` is the natural follow-up; not built now for lack of a
   second caller.

## Architecture & boundaries

Same load-bearing boundary as the tutor-turn substrate: **the graph does the *thinking*; DB
persistence stays in a thin service wrapper.** `AgenticState` stays checkpoint-serializable
(no `AsyncSession`, ORM row, or `LLMClient` in state) even though this slice compiles without a
checkpointer.

```
router (thin SSE mapper)         — app/api/v1/chat.py (mode=="agentic" checked first, ahead of
   └─ run_agentic_turn(...)         the goal/refinement-gate branching)
        — app/services/agentic.py, owns persistence + commit boundaries
        ├─ persist user message (+ commit)
        ├─ graph.astream(state, stream_mode=["custom","values"])   — LangGraph
        │     └─ call_model ⇄ execute_tools, bounded by max_iterations
        │           call_model streams tokens + accumulates tool_calls
        │           execute_tools runs Tool.execute, appends TOOL ChatMessages
        └─ persist assistant message + one summed LLMCall (+ commit)
```

## Module layout & state

- **`app/llm/types.py`** — `ChatRole.TOOL`; `ToolUsePart`/`ToolResultPart` added to `ContentPart`;
  sibling `ToolDef`/`ToolCall` types; `tool_calls` added to `ChatResponse`/`ChatChunk` (additive —
  `content`/`text` stay plain strings).
- **`app/llm/base.py`, `app/llm/registry.py`** — `LLMProvider.complete`/`.stream` and
  `LLMClient.complete`/`.stream` gain `tools: Sequence[ToolDef] | None = None`.
- **`app/llm/providers/{anthropic,openai_compat,fake}.py`** — each provider's own translation of
  `ToolDef`/`ToolUsePart`/`ToolResultPart` to/from its native wire shape (see the finalized plan
  for the exact per-provider request/response/streaming shapes — Anthropic's `tool_use` blocks
  with pre-parsed `input` dicts via `get_final_message()`; OpenAI's index-keyed streamed deltas
  requiring manual JSON accumulation; `FakeProvider`'s new scripted `FakeTurn` sequence).
- **`app/agent/tools.py`** (new) — `Tool`, `ToolResult`, `build_tools(session, llm, *, learner_id,
  subject_id=None) -> list[Tool]`, instantiated fresh per turn (mirrors `build_tutor_graph(llm)`
  closing over the request's client).
- **`app/agent/state.py`** — `AgenticState` (`TypedDict`) + `ToolEvent` (`BaseModel`), alongside
  the existing `TutorState`/`RefinementState`.
- **`app/agent/agentic.py`** (new) — `build_agentic_graph(llm, tools) -> CompiledStateGraph`:
  `call_model` (mirrors `tutor.py`'s `generate` node exactly, plus `tools=` on `llm.stream` and
  `tool_calls` accumulation) ⇄ `execute_tools`, routed by a conditional edge on
  `pending_tool_calls` and `iterations < max_iterations`.
- **`app/services/agentic.py`** (new) — `run_agentic_turn(...)`, mirrors
  `app/services/chat.py::run_tutor_turn` field-for-field.
- **`app/core/config.py`** — `agentic_max_iterations: int = 4`.
- **`app/schemas/chat.py`** — `ChatTurnRequest.mode: Literal["chat", "agentic"] = "chat"`.
- **`app/services/turn_common.py`** — `TurnEvent.type` gains `"tool_call"`.
- **`app/api/v1/chat.py`** — `send_message` checks `data.mode == "agentic"` first; `event_stream()`
  gains a `tool_call` SSE branch.

## Testing

Offline-first throughout (`FakeProvider`/`fake_llm_client`, `db_session` transactional fixture, no
network in default `poe test`):

- **Provider translation (no DB):** `tests/test_llm_tool_use.py` — Anthropic tool-part
  translation + `ChatRole.TOOL` coalescing into one `user` message; OpenAI `tool_calls` field +
  JSON-string arguments + no coalescing; `FakeProvider` scripted two-turn sequence.
- **Tool registry (DB):** `tests/test_agent_tools.py` — seeded chunk content surfaces via
  `search_materials`, learner-scoping holds, empty-query/malformed-args degrade to
  `ToolResult(is_error=True)` without raising.
- **Graph unit (no DB):** `tests/test_agent_agentic.py` — a scripted tool-call turn routes through
  `execute_tools` and loops back; `max_iterations` actually bounds an unbounded-tool-calling
  script (proves the DoD's safety property, not just the happy path).
- **DB e2e:** `run_agentic_turn` against `db_session` — only user+assistant `Message` rows persist
  (no intermediate tool-turn rows), one `tool_call` + one `done` `TurnEvent`, one `LLMCall` with
  usage summed across every `call_model` iteration.
- **Router-level:** `tests/test_chat.py` — `POST /conversations/{id}/messages` with
  `{"mode": "agentic"}` against the real endpoint proves agentic mode bypasses the refinement gate
  even on a goal-less, history-less conversation (the dispatch-ordering decision).

## Dependency

None new — reuses the already-installed `langgraph`, the existing `anthropic`/`openai` SDKs, and
`app/rag/retrieval.py::retrieve`.

## Doc updates (part of this slice)

- **ROADMAP Phase 6** — check the tool-registry/retrieval-as-tool/agentic-mode bullets; add a
  landed-note (final commit of this slice) covering the hand-rolled-loop decision, the one real
  tool, the accepted v1 gaps (no tool-call audit trail, no `stream_graph_tokens` extraction yet).

## Verification

- `uv run poe check` green after every commit in the sequence.
- Manual smoke test via `uv run poe dev` against real Postgres + Ollama: seed a source/chunk,
  start a conversation, send a `mode="agentic"` turn that should trigger `search_materials`,
  confirm the SSE stream shows `tool_call` then `done` with the retrieval reflected in the reply.
  Confirm a plain `mode`-omitted turn is unaffected — the dispatch change must be additive.

## Deferred (later Phase 6 slices)

YAGNI — explicitly out of scope for this slice:

- the live/external-data tool (`app/rag/fetch.py::default_fetch` is the likely primitive)
- the first structured workflow graph (e.g. guided practice / worked-example walkthrough)
- promoting `build_tools()` to a heavier `ToolSpec`/`ToolContext` registry
- extracting `stream_graph_tokens` to de-duplicate the `astream`-mode-dispatch boilerplate across
  `run_tutor_turn`/`run_refinement_turn`/`run_agentic_turn`
- a `ToolCall` audit table for durable intra-loop tool-call/tool-result history
- promoting `mode` from a per-turn field to a persisted `Conversation.mode` (tier-gating)
