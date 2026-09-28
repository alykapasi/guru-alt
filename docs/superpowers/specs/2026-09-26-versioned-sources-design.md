# Versioned sources: durable citations and resumable reindexing (S29, S50)

**Status:** approved in conversation 2026-09-26; this document records it.
**Tracker:** S29 (preserve historical citations across source revisions), S50 (versioned
reindexing and embedding migration).

## Problem

- Re-ingesting a source (`POST /sources/{id}/retry`, or re-uploading a failed one) runs
  `pipeline.run`, which deletes every chunk of the source and writes new ones with new ids.
  Two things hold chunk ids: `messages.citations` and `content_blocks.citations`. After a
  re-ingest every old `[N]` points at nothing — the citation pane shows "Unknown source" and an
  empty passage, and the paid support checker silently drops the citation.
- A DONE source is re-ingested on request with no warning that anything will change.
- Changing the embedding model leaves old chunks out of the vector arm (safe, by
  `embedding_space`) but the only way to bring them back is retrying sources one by one, which
  also breaks their citations.
- Nothing records which extraction/chunking version produced a chunk, so an extraction
  improvement cannot tell which sources are stale.
- Legacy sources may carry a topic from a different subject (S55 only validates new
  assignments); S50 asks the migration to validate or repair that.

## Decisions (from the design conversation)

1. **Keep superseded chunks that something cites** (choice A). Re-ingest supersedes rather than
   deletes: cited chunks stay with their text and locator, uncited ones are deleted. Deleting a
   source still deletes everything.
2. **Re-ingesting a DONE source requires the learner's confirmation.** The source already exists
   in the system; only after an explicit yes do its chunks get new ids. A FAILED source has
   nothing to replace and retries without asking.
3. **Operator reindex** (choice A): dry run by default; `--apply` re-embeds in place (ids kept);
   re-extraction only under a separate `--reextract` flag.

## Design

### 1. Data model and superseding

**Migration 0064** on `chunks`:

- `superseded_at timestamptz NULL`
- `pipeline_version integer NOT NULL DEFAULT 1` (server default; every existing chunk is 1)
- `embedding` becomes nullable (a superseded chunk has none)

`PIPELINE_VERSION = 1` in `app/rag/pipeline.py`, stamped on every chunk `pipeline.run` writes.
It is bumped whenever extraction or chunking changes in a way that changes chunk text.

**`supersede_chunks(session, source_id) -> tuple[int, int]`** (in `app/rag/pipeline.py`) replaces
the `delete(Chunk)` in `pipeline.run` and returns `(kept, deleted)`:

1. the source's current chunk ids (`superseded_at IS NULL`);
2. which of them are cited: any id appearing as `chunk_id` in `messages.citations` or
   `content_blocks.citations` (JSONB, via `jsonb_array_elements`);
3. uncited → deleted (their KC tags cascade, as today);
4. cited → `superseded_at = now()`, `embedding = NULL`, KC tags (`chunk_kcs`) deleted; text,
   ordinal, locator and provenance kept.

Already-superseded chunks of the source are left alone (they stay cited history).

**Readers see only current chunks.** Both retrieval arms, `retag_source`, and any count of a
source's chunks filter `superseded_at IS NULL`. `GET /chunks/{id}` still returns a superseded
chunk to its owner; `ChunkRead` gains `superseded: bool`. The support checker
(`content.check_block_citations`) reads cited chunks by id, superseded or not.

### 2. Confirmation before re-ingesting

`POST /sources/{id}/retry` takes an optional body `{"confirm": bool}` (default false).

- Source DONE and not confirmed → **409** with `detail = {"code": "confirm_required",
  "message": "This source is already in your library. Re-processing it replaces its passages;
  older replies will show their citations as an earlier version."}`. Nothing changes.
- Source DONE and confirmed → reset and enqueue (today's behaviour).
- Source FAILED (or PENDING with no live claim) → reset and enqueue without confirmation.
- Live claim → **409** with `detail = {"code": "ingesting", "message": "This source is being
  ingested right now."}` (today's message, now with a code).

The re-upload path (`create_or_reuse_source`) is unchanged: it only ever resets FAILED sources.

### 3. Operator reindex

`uv run poe reindex [--apply] [--reextract] [--learner ID] [--limit N]`
(`app/workers/reindex.py` CLI, logic in `app/services/reindex.py`).

**Staleness is derived from the data on every run** — no progress table; an interrupted run is
resumed by running it again.

- **re-embed:** DONE source with ≥1 current chunk whose `embedding_space` ≠ the current space.
- **re-extract:** DONE source with ≥1 current chunk whose `pipeline_version` < `PIPELINE_VERSION`.
- **scope repair:** source whose `topic_id` is set and either belongs to a different subject
  than `subject_id`, or is not visible to the source's learner. Repair: if `subject_id` is NULL
  and the topic is visible, set `subject_id` to the topic's subject; otherwise clear `topic_id`.
  A `subject_id` not visible to the learner is cleared too.
- Sources with no current chunks (text duplicates, failures) are not re-embedded or
  re-extracted.

`--learner ID` restricts every group to one learner; `--limit N` caps how many sources the
re-embed and re-extract steps touch in this run (scope repairs are cheap and uncapped).

**Dry run (default):** changes nothing; prints each group with source ids, origins, chunk
counts and the reason, plus totals.

**`--apply`:**

- scope repairs are written (one commit);
- each re-embed source: embed its current chunks' text with `embed_in_batches` (existing batch
  size and concurrency), write the new vectors and `embedding_space` onto the same rows, log the
  EMBED cost against the source's learner, commit. A failure is logged, rolled back, reported in
  the summary, and the run continues.

**`--apply --reextract`:** additionally, each re-extract source is reset with
`reset_for_reingest` and dispatched to the ingestion queue (the operator's flag is the explicit
yes). Sources with a live claim are skipped and listed. A source needing both is re-extracted
(and not also re-embedded). Without `--reextract`, a re-extract source that also needs
re-embedding is re-embedded so it stays searchable.

Exit code 0 when the run completed (individual failures are reported, not fatal); 2 on bad
arguments.

### 4. Frontend

- **Source list** (`frontend/src/components/uploads/SourceList.tsx`): FAILED rows get **Retry**
  (posts directly); DONE rows get **Re-process**, which opens a confirm dialog with the message
  above and posts `{"confirm": true}` only on yes. Hidden while pending/processing.
- **Citation pane** (`CitationPane.tsx`): a superseded chunk shows its passage under "From an
  earlier version of this source"; a missing chunk (404) shows "This passage is no longer
  available." instead of "Unknown source" with empty text.
- Regenerate `frontend/src/api/schema.d.ts`.

### 5. Documentation

RUNBOOK §15: what `PIPELINE_VERSION` means and when to bump it; the order dry run → `--apply`
→ `--apply --reextract`; that re-embedding keeps citations and re-extraction supersedes.

## Testing

- Supersede: re-ingesting a source with one cited and one uncited chunk keeps the cited one
  (text kept, embedding NULL, KC tags gone, `superseded_at` set) and deletes the other; a
  citation in a content block counts as well as one in a message.
- Retrieval and retagging never return superseded chunks; deleting the source removes them.
- The support checker still reads a superseded cited chunk; `GET /chunks/{id}` returns it with
  `superseded: true`.
- Retry: DONE without confirm → 409 `confirm_required`, status unchanged, nothing enqueued; DONE
  with confirm → reset and enqueued; FAILED without confirm → reset; live claim → 409
  `ingesting`.
- Reindex: dry run changes nothing and lists all three groups; `--apply` re-embeds in place
  (same ids, new space, cost row); a second run finds nothing; one source's embed failure does
  not stop the next; `--limit` respected; `--learner` restricts; `--reextract` resets and
  enqueues and skips a live claim; both scope repairs.
- Migration: existing chunks come through with `pipeline_version = 1` and not superseded.
- Frontend (vitest): Retry posts directly; Re-process asks first and posts `confirm: true` only
  on yes; citation pane's two new states.

## Out of scope

Automatic reindex on deploy; a learner-facing reindex; keeping vectors of superseded chunks;
repairing citations already broken before this change; S27 extraction changes themselves.

## Tracker updates on completion

- S29 → Completed.
- S50 → Partial; remaining: run the reindex against a real corpus, and bump `PIPELINE_VERSION`
  when S27's extraction work lands (testing phase).
