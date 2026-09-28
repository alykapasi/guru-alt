# Archive, delete and forget (S61, S42; V11)

**Status:** approved in conversation 2026-09-26; this document records it.
**Tracker:** S61 (archive/delete/forget half), S42 (deletion with optional forgetting).
**Decision:** V11 — distinguish archiving from deletion; an explicit action also forgets derived
learning, backed by provenance; ordinary deletion explains what it keeps.
Workstream 4, slice A. Account lifecycle and retention windows (V12) are slice B.

## Problem

- A learner cannot remove an uploaded source at all: there is no delete endpoint.
- Deleting a conversation keeps its memories and sets their `conversation_id` to NULL, so
  nothing can later say which memories came from it, and nothing offers to forget them.
- Nothing can be put away without being destroyed. Goals can be archived; sources and
  conversations cannot.
- Provenance is thin. Lessons (content blocks) cite chunks and sources in JSON; generated
  questions record no source (and are generated without retrieval); learning events carry no
  conversation or source link.

## Decisions (from the design conversation)

1. **Forget removes derived artifacts, never demonstrated ability** (choice A). Forgetting a
   conversation removes the memories drawn from it; forgetting a source removes the lessons and
   content built on its passages. Answers, learning events, KC states and items stay: they are
   evidence of what the learner can do, whatever material it came through.
2. **Archive is out of the way and out of use, fully reversible** (choice A). An archived
   source is never retrieved; an archived conversation is read-only. Nothing is deleted.
3. **Delete is immediate, after a confirmation that shows what stays**, with an "also forget"
   option (choice A). Archive is the reversible path, so delete is final.
4. **One removal service with a per-kind provenance resolver** (approach 1), following the
   one-scope-resolver pattern of S26/S28. Slice B's account flows call the same service.

## Design

### Data — migration 0066

- `sources.archived_at timestamptz NULL`, `conversations.archived_at timestamptz NULL`.
- `memories.origin_conversation_id uuid NULL` — **no foreign key**, backfilled from
  `conversation_id`, and set wherever a memory is created or corrected alongside
  `conversation_id`. The FK still nulls on conversation delete; the origin survives it, which is
  what lets forgetting work after a delete.

### `app/services/removal.py`

```python
Kind = Literal["source", "conversation"]

@dataclass(frozen=True)
class Impact:
    kept: dict[str, int]         # what an ordinary delete leaves in place, by label
    forgettable: dict[str, int]  # what forget=True would remove, by label
    notes: list[str]             # plain sentences for the dialog

async def impact(session, learner_id, kind, target_id) -> Impact | None
async def archive(session, learner_id, kind, target_id) -> bool
async def unarchive(session, learner_id, kind, target_id) -> bool
async def delete(session, learner_id, kind, target_id, *, forget: bool) -> Impact | None
async def forget_conversation_memories(session, learner_id, conversation_id) -> int
```

`None` / `False` means "not found or not yours" — the route answers 404 for both. Refusals raise
`RemovalRefused(code)` → 409 `{"code": code}`.

**Provenance resolvers** (one per kind, the only place that knows what derives from what):

- *Source* → content blocks of this learner whose `citations` contain `{"source_id": <id>}`
  (JSONB containment); chat messages whose citations name it (kept, counted).
- *Conversation* → current memories whose `origin_conversation_id` is the conversation.

### Archive

- Sets/clears `archived_at`; nothing else changes. Owner only.
- **Sources:** `resolve_scope` excludes archived sources, so chat grounding, lessons, the
  agent's `search_materials` and anything else built on the scope never read them. Onboarding's
  curriculum retrieval and any other direct reader of a learner's sources applies the same
  filter. `GET /sources` hides archived sources unless `?archived=true` (which lists only them).
  Retry/re-process of an archived source → 409 `archived`. An archived source mid-ingest still
  finishes; it is simply not retrieved.
- **Duplicates:** an archived original no longer answers for its duplicates, so `_stranded`
  (S77) treats an archived original as absent and the sweep releases them; the twin check
  likewise ignores archived sources. **Unarchive re-deduplicates:** if a same-text source
  answers in the same scope (current chunks, not archived), the unarchived source becomes its
  duplicate — `duplicate_of_id` set, own chunks superseded — using the stored `text_sha256`, no
  extraction. Otherwise both would answer.
- **Conversations:** `GET /conversations` hides archived ones unless `?archived=true`. An
  archived conversation's transcript still loads; posting a message → 409 `archived`. Its
  memories stay current.

### Impact and delete

`GET /sources/{id}/removal`, `GET /conversations/{id}/removal` → `Impact`.
`DELETE /sources/{id}?forget=`, `DELETE /conversations/{id}?forget=` → `Impact` of what was done
(status 200; the existing conversation DELETE keeps its path and gains the query parameter and
body).

- **Source delete:** refused with 409 `ingesting` while a live lease is held. Deletes the row;
  chunks (cited history too), KC tags and conversation links cascade; duplicates' FK nulls and
  the sweep releases them. The blob is removed after commit only when no other source shares its
  `blob_key` (the account-deletion guard); a failed blob delete is logged and named in the
  response notes (the retryable orphan sweep is slice B).
- **Conversation delete:** as today (messages and turns cascade); memories keep their origin.
- **`forget=true`**, same transaction: the source's citing content blocks are deleted; the
  conversation's current memories are marked `DELETED` (the existing soft delete, so
  re-extraction recognises and does not revive them); the learner profile's
  `evidence_watermark` is cleared so the next refresh recomputes from what remains.
- Never touched: learning events, KC states, items, rubrics, subjects, lesson plans.
- **Notes** (examples): "Replies that cited this file will show it as no longer available."
  "The subject built from this file stays." "Your answers and progress stay — they are evidence
  of what you can do." "Memories from this conversation stay; you can forget them later from
  Memory."

`POST /memory/forget-origin/{conversation_id}` → `{"forgotten": n}`: the same memory rule for a
conversation that is live, archived or already deleted (matched by `origin_conversation_id` and
learner). `MemoryRead` gains `origin_conversation_id` and `origin_title: str | None` (title of
the live conversation, else None → "From a deleted conversation").

### Frontend

- Uploads list: a per-row menu (Archive, Delete…); an "Archived" disclosure listing archived
  sources with Unarchive and Delete….
- Chat sidebar: the same menu per conversation, an archived disclosure, and a read-only banner
  with "Unarchive to continue" on an archived conversation.
- `RemovalDialog` (shared): loads the impact; shows kept counts and notes; an "Also forget what
  was learned from this" checkbox listing what would go; Delete; inline refusal messages.
- Memory page: origin label per memory and "Forget all from this conversation".

### Errors

404 for anything not the learner's (every new route added to the visibility sweep); 409
`ingesting` for a mid-ingest delete; 409 `archived` for posting to an archived conversation or
retrying an archived source.

## Testing

- Migration: `origin_conversation_id` backfilled from `conversation_id`.
- Archive: an archived source is excluded from chat grounding, lessons, the agent tool and
  onboarding retrieval; lists hide it and `?archived=true` shows it; unarchive restores retrieval
  with the same chunk ids; unarchive re-deduplicates against an answering twin; an archived
  original's duplicate is released by the sweep; an archived conversation refuses a message;
  an archived source's retry → 409.
- Delete: impact counts; a plain delete keeps lessons and memories; memories keep their origin
  after the conversation is deleted; `forget=true` deletes citing blocks and marks memories
  DELETED, and write-back does not revive them; the profile watermark is cleared; learning
  events and KC states are untouched; a shared blob survives, an unshared one is removed;
  mid-ingest delete → 409; forget-by-origin after delete; another learner's ids → 404.
- Frontend (vitest): dialog states (counts, notes, checkbox, refusal), menus, archived sections,
  memory origin label.

## Out of scope

Account deletion lifecycle, recovery window, erase-now, retention windows and the orphan-blob
sweep (slice B); recomputing mastery from remaining evidence; bulk archive or delete; archiving
subjects or goals; forgetting chat messages' citations (they simply show the passage as gone).

## Tracker updates on completion

- S61: archive/delete/forget half done; remaining V12 account lifecycle, retention windows,
  orphan cleanup, export of uploaded files (slice B).
- S42: deletion with optional forgetting done; remaining contradiction-vs-coexistence
  supersession (slice D).
