# Conversation scope + citations — design

## Context

Phase 7 (frontend MVP) just landed a working chat UI (sidebar, streaming, chat/agentic modes,
rename/delete). Aligning on the next slice surfaced three related gaps, confirmed against the
actual code before designing around them:

- **Citations exist for content-block generation** (`ContentBlock.citations`, lessons/wikis/
  questions in `app/services/content.py`) but **not for any conversational mode** — the plain
  tutor turn (`run_tutor_turn`) never calls `retrieve()` at all; the agentic `search_materials`
  tool retrieves chunks but discards them after formatting into a string for the model, so
  nothing survives to cite; the workflow mode's worked-example generation doesn't ground either.
- **Retrieval scoping already exists at the query layer** — `retrieve()` accepts `subject_id`/
  `topic_id`/`source_id`, and `Source` already carries optional `subject_id`/`topic_id` set at
  upload time. What's missing is a `Conversation`-to-`Source` link and a UI to set it.
- **Memory is already learner-global, not subject-scoped** (Phase 5) — the "a mnemonic from
  studying art history should still surface while studying physics" behavior the user wants is
  already the architecture's split. Only the *content* (RAG chunks) needs a hard subject
  boundary; memory needs none.

Decisions locked in the alignment conversation (see MASTERPLAN.md §4.9, §5, §7 and ROADMAP.md
Phase 7 for the plan-level record):

- Citations render as **inline numbered markers** (`[1]`, `[2]`) tied to specific sentences —
  not a flat "sources" chip row.
- **All three generation modes** (chat, agentic, workflow) get citations in this slice.
- Conversation creation becomes a **modal**: pick a subject or "General" (no library grounding),
  then optionally narrow to specific sources within that subject's materials.
- v1 citation click-through shows the cited chunk's own extracted text + locator in a pane — not
  a re-rendered original-format file viewer.

## Data model

**`ConversationSource`** (new join table) — many-to-many, `(conversation_id, source_id)` unique.
Empty for a conversation = "all sources under its subject" (the common case); populated = a
learner-narrowed subset. Cascades on either side's delete.

**`Message.citations`** (new `JSONB` column, default `[]`) — mirrors `ContentBlock.citations`'
shape exactly: `[{"marker": int, "chunk_id": str, "source_id": str}, ...]`. `marker` is the
literal `N` from the `[N]` that appears in `Message.content` — the frontend doesn't recompute
order, it looks up the marker it finds in the text directly. Thin references (not the chunk text
inline) — same reasoning as `ContentBlock`: the pane fetches full text on click, keeping message
rows small.

**`ConversationCreate`** gains `source_ids: list[UUID] = []`. **`ConversationRead`** gains
`source_ids: list[UUID]` (so the frontend can reflect what a conversation is scoped to). Server
validates: every `source_id` must belong to the learner and to the chosen `subject_id` (404/400
otherwise); `source_ids` must be empty when `subject_id` is `None` ("General" has nothing to
narrow).

## The citation-marker protocol (shared, `app/services/turn_common.py`)

Two new pure functions, used identically by all three generation call sites:

```python
def format_grounding(hits: Sequence[RetrievalHit]) -> str | None:
    """Numbered passages for the system prompt, or None if hits is empty (omit the section
    entirely rather than an awkward empty grounding block)."""

def extract_citations(reply: str, hits: Sequence[RetrievalHit]) -> list[dict]:
    """Scan `reply` for [N] markers, map each to hits[N-1], and return only the markers the
    model actually used — never inventing rows for numbers the model never wrote, and silently
    ignoring out-of-range numbers (a hallucinated [7] with only 3 hits provided)."""
```

System-prompt instruction paired with `format_grounding`'s output: cite inline immediately after
a sentence that draws on a passage (`"...as shown here [1]."`), only cite passages actually
used, never invent a number. This is a *grounding-set* claim (like `ContentBlock.citations`
already is), not a verified-per-sentence-correct claim — the model could still misattribute
within that constraint, same honesty bar the existing content-block citations already accept.

**Markers stay in the stored text.** `Message.content` literally contains the `[N]` — no
markdown/AST rewriting. The frontend regex-parses `content` for `[N]` at render time and maps
found markers against the `citations` array; a marker with no matching citation entry (the
hallucinated-number case) renders as inert plain text, not a broken link.

## Wiring per mode

**Chat (`run_tutor_turn`, `app/services/chat.py`).** If `conversation.subject_id` is set, call
`retrieve()` (query = the learner's message, scoped by `subject_id` + the conversation's linked
`source_ids`, `limit=settings.chat_grounding_limit` — a new setting, default `5`, smaller than
`retrieve()`'s general default since this runs on every turn and system-prompt length matters
more here than in a one-off tool call) before building the
system prompt. Insert `format_grounding(hits)` between plan-grounding and the memory-note in the
existing pinned order. After streaming, `extract_citations(reply, hits)` and pass to
`add_message(..., citations=...)`. "General" conversations (`subject_id is None`) retrieve
nothing — unchanged from today.

**Agentic (`run_agentic_turn`, `app/agent/tools.py`).** The harder case: the model may call
`search_materials` zero, one, or several times in one turn, and citation numbers must stay
*stable and deduped* across all of them (the same chunk retrieved twice must keep one marker,
not two). `_search_materials_tool`'s closure gains a shared, turn-scoped accumulator (a
`dict[chunk_id, RetrievalHit]` built up across calls, closed over by `run_agentic_turn` rather
than module state — mirrors how `build_tools` already closes over the request's `session`/`llm`
per turn, never global). `_format_hits`'s tool-result text switches to the same `[N]`-numbered
format `format_grounding` produces, sourced from the accumulator's current state at call time,
so the model sees consistent numbering turn-over-turn within one agentic loop. After the loop
ends, `extract_citations` runs against the full accumulated hit list. `build_tools` and
`_search_materials_tool` gain a `source_ids: Sequence[uuid.UUID] | None` param, threaded from
the conversation the same way `subject_id` already is.

**Workflow (`present` step only, `app/agent/workflow.py`).** `present` generates the worked
example + practice problem for the plan's active KC — the one workflow step that's actually
"teaching from materials," unlike `respond` (feedback on the learner's attempt, not source-
grounded). Retrieve scoped by the conversation's `subject_id`/`source_ids` (workflow presupposes
a committed goal+plan, so `subject_id` is always set by the time workflow runs) using the active
KC's name as the query, fold into `present`'s generation the same way as chat, extract citations
on the persisted message. `respond` is explicitly out of scope for citations this slice.

## New endpoints

- `GET /sources` — list the learner's sources, optional `?subject_id=` filter. Backs the
  creation-modal source picker.
- `GET /chunks/{chunk_id}` — single chunk fetch (`ChunkRead` — already has `text` + `provenance`,
  nothing new needed on that schema). Backs the citation pane's click-through. 404s if the
  chunk's source doesn't belong to the learner.

`retrieve()` gains `source_ids: Sequence[uuid.UUID] | None = None` alongside the existing
singular `source_id` (left untouched for its current callers) — when provided, filters
`Chunk.source_id.in_(source_ids)`.

## Frontend

- **Creation modal**: replaces the instant "New chat" click. Step 1: subject picker (`GET
  /subjects`) or "General". Step 2 (subject chosen only): optional multi-select source picker
  (`GET /sources?subject_id=`) — skipping it means "all sources in this subject." Confirm calls
  `POST /conversations` with `subject_id` + `source_ids`.
- **`MessageBlock`** gains an optional `citations` prop. Regex-scans `content` for `[N]`;
  matched markers with a corresponding citation render as a small clickable superscript, opening
  the citation pane; markers with no match render as plain text. During live streaming, `done`'s
  citations haven't arrived yet, so markers render inert until the turn finishes — a minor,
  acceptable wrinkle (matches how the message itself isn't "final" until `done` either).
  `TurnEvent`'s `done` variant and `MessageRead` both gain `citations`.
- **`CitationPane`**: a slide-over showing the clicked chunk's `text` + its source's `origin` +
  `provenance` locator (page/slide/timestamp/etc., whatever the adapter recorded), fetched from
  `GET /chunks/{id}` on open.

## Testing

- Backend: `retrieve()` multi-source filtering; `format_grounding`/`extract_citations` unit
  tests (empty hits, no markers used, out-of-range marker, repeated marker); end-to-end chat/
  agentic/workflow tests asserting citations land on the persisted message and match what the
  `FakeProvider`'s scripted reply actually cited; `ConversationSource` validation (source not
  owned by learner, source belongs to a different subject, source_ids on a "General" conversation
  all 400/404 as appropriate); new endpoint tests for `GET /sources` and `GET /chunks/{id}`.
- Frontend: existing `npm run lint`/`build` gate; live browser verification of the creation
  modal and citation click-through once a real model is available locally (blocked today on the
  same `llama3.2`-not-pulled gap noted in the prior slice — citations can still be verified via
  backend tests and a scripted `FakeProvider` reply containing `[1]` even without live
  generation).

## Known v1 scoping (explicit, not oversights)

- Citations are a grounding-set claim, not a verified-correct-attribution claim — same honesty
  bar `ContentBlock.citations` already accepts.
- The refinement gate (goal negotiation) gets no citations — it isn't teaching content.
- Workflow's `respond` step gets no citations — feedback prose, not source-grounded.
- The citation pane shows extracted chunk text, not the original file re-rendered at that exact
  page/timestamp — MASTERPLAN §7's "Citation display" decision; a native viewer is additive
  later work, not blocked by anything built here.
