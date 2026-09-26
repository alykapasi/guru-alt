# Source scope, sources-only mode and honest support (S26, S28)

**Status:** approved in conversation 2026-09-26; this document records it.
**Tracker:** S26 (consistent source scope and sources-only mode), S28 (explain source support
and insufficiency). Governing decision: V05 in `docs/V0_DECISIONS.md` — "General knowledge may
supplement learner-selected sources with clear attribution. Offer a sources-only mode.
Unassigned material is not silently added to an unrelated conversation."

## Problem

Five code paths retrieve from a learner's sources, and each decides scope on its own:

| Path | Scope today |
|---|---|
| Lesson content blocks (`app/services/content.py`) | subject **plus untagged** sources (`include_untagged_sources=True`) |
| Tutor chat (`app/services/chat.py`) and practice (`app/services/workflow.py`) | subject only; optionally the conversation's picked sources |
| Agentic `search_materials` (`app/agent/tools.py`) | with no subject — a "General — no library grounding" chat — searches **every** source the learner owns, contradicting the label |
| Onboarding curriculum (`app/services/onboarding.py`) | explicitly picked sources, one retrieval each |
| Debug `/retrieve` (`app/api/v1/sources.py`) | whatever the caller passes |

Attribution is equally uneven: only lesson blocks have an "ungrounded" prompt that says the text
does not come from the learner's materials. Chat replies carry `[N]` markers but nothing tells the
learner how much of a reply their sources actually carried, and there is no sources-only mode.

## Decisions (from the design conversation)

1. **Untagged sources are opt-in per subject** (choice C). Default off; a subject's owner can
   turn on "also use my untagged materials".
2. **Sources-only is a per-subject setting** beside the untagged switch (choice A). Lessons,
   chat, practice and agent search all read it.
3. **Attribution is a server-computed coverage label plus a spoken note** (choice A). The label
   is derived from facts the server holds (what was retrieved, which markers the reply cited),
   never from the model's self-report. The prompt makes the tutor say in words when it goes
   beyond the passages.
4. **Architecture: one scope resolver and one grounding policy** used by every path (approach 1).

## Design

### 1. Data and scope

**Migration 0063** adds to `subjects`:

- `include_untagged_sources boolean not null default false`
- `sources_only boolean not null default false`

No backfill. Existing subjects get both off, so lesson generation stops admitting untagged
sources until the owner opts in — that is the V05 behaviour, not a regression.

**Endpoint** `PATCH /api/v1/subjects/{subject_id}/source-settings`, body
`{"include_untagged_sources": bool | null, "sources_only": bool | null}` (a null field is left
unchanged), returns the subject. Authorization:

- subject not visible to the learner → 404 (same as every other subject route);
- visible but curated (`owner_learner_id IS NULL`) → 403; curated subjects keep the defaults;
- otherwise `knowledge_svc.is_writable_by` must hold.

`SubjectRead` exposes both fields so the frontend can render the switches.

**`app/rag/scope.py`** (new):

```python
@dataclass(frozen=True)
class SourceScope:
    learner_id: uuid.UUID
    subject_id: uuid.UUID | None = None
    topic_id: uuid.UUID | None = None
    source_ids: tuple[uuid.UUID, ...] = ()
    include_untagged: bool = False
    sources_only: bool = False

async def resolve_scope(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    source_ids: Sequence[uuid.UUID] = (),
) -> SourceScope | None: ...
    """``None`` means no library at all (a General conversation): callers skip retrieval."""
```

Rules:

- `subject_id is None` and no `source_ids` → `None`: no retrieval (this closes the agentic
  General-chat leak). A `SourceScope` with neither a subject nor sources is still meaningful —
  the learner's whole library — and only the debug endpoint builds one.
- `subject_id` given → reads the two switches from the subject row. When `source_ids` is
  non-empty the scope narrows to exactly those and `include_untagged` is forced `False` — the
  learner already chose.
- `source_ids` without a subject (onboarding only) → exactly those sources; switches default.

**`retrieve()`** changes signature to
`retrieve(session, llm, query, *, scope: SourceScope, limit=10, candidates=50)`. The loose
`learner_id / subject_id / topic_id / source_id / source_ids / include_untagged_sources`
parameters go away. The debug `/retrieve` endpoint builds a `SourceScope` from its request
(`subject_id`, `topic_id`, and its single `source_id` as `source_ids=(source_id,)`), keeping its
current behaviour. `retrieve` itself has no notion of "no library"; that is `resolve_scope`
returning `None`.

Callers after the change:

| Path | Scope |
|---|---|
| Lessons | `resolve_scope(learner, kc's subject)` |
| Chat / practice | `resolve_scope(learner, conversation.subject_id, conversation.source_ids)` |
| Agent search | same as chat; `None` → the tool answers "No materials are in scope for this conversation." without querying |
| Onboarding | `SourceScope(learner_id, source_ids=(id,))` per picked source |
| Debug `/retrieve` | built from query params |

### 2. Grounding policy and coverage

**`app/services/grounding.py`** (new) absorbs `GROUNDING_INSTRUCTION` and `format_grounding` from
`app/services/turn_common.py` (callers updated; no re-export shim).

`format_grounding(hits, *, sources_only: bool) -> str` always returns a section when there is a
scope — the empty-retrieval case now has something to say. Callers whose scope is `None` do not
call it. The instruction is one of four fixed strings:

| | Passages retrieved | Nothing retrieved |
|---|---|---|
| **Normal** | Use the passages and cite `[N]`. If you go beyond them, say so in a plain sentence. If passages disagree, say so and cite both rather than choosing one. | Say plainly that the learner's materials had nothing on this, then answer from general knowledge. |
| **Sources-only** | Answer only from the passages. If they do not cover part of the question, name that part and do not answer it from general knowledge. Disagreeing passages: same rule as normal. | Say the learner's materials do not cover this. Suggest adding a source or turning off sources-only for this subject. Do not answer from general knowledge. |

The passages stay fenced with `as_untrusted` (S31) exactly as today.

The agentic path appends the same instruction (via `grounding.policy_note(scope)`) to its
system prompt when there is a scope, because its passages arrive in tool results, not in
the system prompt.

**Lessons.** `content.py` keeps its JSON-output prompts but takes the grounded/ungrounded
distinction and the sources-only rule from the same policy:

- passages retrieved → existing `_SYSTEM_PROMPT`, extended with the "say so if you go beyond"
  and (sources-only) "only from the passages" clauses;
- nothing retrieved, normal → existing `_UNGROUNDED_SYSTEM_PROMPT`;
- nothing retrieved, sources-only → `generate_block` raises `NoSourceCoverage(kc_id)` before any
  model call; nothing is cached, logged or paid for. The content API maps it to HTTP 422 with
  detail `"Your sources for this subject don't cover this concept."`.

**Practice** item selection and grading are unchanged: items come from the knowledge graph, and
sources-only governs explanatory text only.

**Coverage** is derived, never generated:

```python
class Coverage(StrEnum):
    CITED = "cited"                       # the reply cited ≥1 retrieved passage
    RETRIEVED_NOT_CITED = "retrieved_not_cited"
    NONE = "none"                         # nothing retrieved (or no search made)

def coverage(grounding_count: int | None, citations: Sequence[dict]) -> Coverage | None:
    """None when there was no library scope (General chat, or data from before S26)."""
```

- New nullable column `messages.grounding_count` (same migration). Chat, practice and agentic
  assistant messages store `len(hits)` (agentic: the accumulated hit count; 0 if it never
  searched). NULL for General conversations, refinement replies and every pre-existing row.
- `MessageRead` and `ContentBlockRead` gain `coverage: Coverage | None` and
  `cited_source_count: int` (distinct `source_id`s in `citations`), both computed from stored
  fields.
- Labels (frontend): `cited` → "Draws on N of your sources"; `retrieved_not_cited` → "Your
  materials were searched but not used"; `none` → "Not from your materials".

"Partly from your sources" is deliberately not a label: telling partial from full coverage is a
judgement about the text. The tutor's own sentence carries it; a Jev `fully_sourced` question
could sharpen the label later in shadow mode (recorded under S80).

### 3. Frontend

- `SourceSettingsPanel` on the Lessons page for the selected **private** subject, beside
  `ConnectionsPanel` and `PublishPanel`: two toggles with one-line explanations, hidden for
  curated subjects. New hook `useUpdateSourceSettings`.
- `CoverageChip` under assistant messages in `MessageBlock` when `coverage` is non-null.
- Upload form hint: untagged sources are only used by subjects that opt in.
- No lesson-block viewer exists in the frontend; lesson coverage and the 422 are API-only in this
  slice.
- Regenerate `frontend/src/api/schema.d.ts` (`poe api-contract`).

## Testing

- `resolve_scope`: untagged excluded by default and included when switched on; picked sources
  override the switch; General → `None`; onboarding scope.
- `retrieve`: existing scope tests move to `SourceScope`; one case per switch; the debug endpoint
  keeps its subject/topic/source filtering.
- Agent search in a General conversation returns the "no materials in scope" result and does not
  retrieve.
- `grounding`: the four instruction cells; `coverage()` for all states including `None`.
- Chat and practice turns store `grounding_count`; practice uses the same instruction text.
- Lessons: sources-only with nothing retrieved raises `NoSourceCoverage`, caches nothing, logs no
  LLM call; the API returns 422; untagged sources are no longer used by default.
- Settings endpoint: owner succeeds, stranger 404, curated 403, partial body leaves the other
  field unchanged.
- Frontend (vitest): chip renders the three labels and nothing for null; panel toggles call the
  hook; panel hidden for curated subjects.

## Out of scope

Per-conversation overrides; sentence-level general-knowledge markers; automatic conflict
detection (conflicts stay prompt-driven); backfilling `grounding_count` for old messages; a
lesson-block viewer; support-checker accuracy (S59).

## Tracker updates on completion

- S26 → Completed.
- S28 → Partial; remaining: checker accuracy (S59) and showing lesson coverage once a
  lesson-block viewer exists.
- S80: add `fully_sourced` as a candidate Jev question.
