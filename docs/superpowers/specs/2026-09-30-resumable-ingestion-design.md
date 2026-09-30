# Resumable, strictly bounded ingestion (S37, remainder)

**Status:** approved in conversation 2026-09-30; this document records it.
**Tracker:** S37 (its remaining part), plus the S49 note that concept tagging swallows a
provider outage. Workstream 5, piece 3 (after S47, S49; before S17, S62, S53).

## Problem

`ingestion.ingest_source` claims a source and runs `pipeline.run` as one job, in one database
transaction, under `ingest_job_timeout_seconds` (3600):

- extract (download, adapter — which may OCR pages or transcribe audio through paid models),
- chunk and embed every chunk,
- write chunks and tag them with concepts.

Any failure rolls the whole job back, so a retry pays again for every OCR page, transcript and
embedding that had already succeeded. `_tag_chunks` swallows every error per chunk
(`kc_tagging.tag_chunk`), so a provider outage leaves a source `done` with no concept tags,
never retried. `ingest_max_concurrent_jobs` is a soft cap (a subquery in the claim's UPDATE; two
racing claims can both take the last place), and one learner's many uploads can occupy every
place.

## Decisions (from the design conversation)

1. **No per-job spend ceiling** in this piece. Per-learner daily caps (S47) and the existing
   character and chunk budgets bound an upload; the ceiling stays a tracked follow-up.
2. **Checkpointed stages in one job** — not one queued job per stage.
3. **Exact slots plus a per-learner cap**, taken as Postgres advisory locks; no deployment-wide
   model-call ceiling.

## Behaviour

### Stages

`sources.stage` (Text, nullable) records where the job resumes. NULL means nothing to resume
(never started, or finished).

1. **`extract`** (NULL → `embed`). Download, run the adapter, compute `text_sha256` / `simhash`
   and the same-text duplicate check exactly as now. The extracted units are written to the
   blob store at `ingest/{source_id}/extract.json.gz` (gzip JSON list of `ExtractedUnit`).
   Commit `stage = "embed"`. A same-text duplicate ends here as today (supersede its own
   chunks, mark `done`, `stage` NULL, no artifact kept). The character budget
   (`ingest_max_extracted_chars`) is checked before the artifact is written.
2. **`embed`**. Read the artifact, chunk it (deterministic), check `ingest_max_chunks`, then
   embed the chunk ordinals not already in `staged_chunks` for this source, in
   `embed_batch_size` batches with `embed_concurrency` in flight. Each completed batch's rows are
   inserted into `staged_chunks` and committed. When every ordinal is staged, continue.
3. **`publish`** — one transaction: supersede the source's current chunks (cited ones kept,
   S29), `INSERT INTO chunks … SELECT … FROM staged_chunks` (fresh ids, same columns as today's
   chunk rows), delete the staged rows, mark the source `done` with `chunk_count`, set
   `stage = "tag"`, reset `attempts` to 0, release the lease. Then delete the artifact
   (best-effort; a failed delete is logged and retried by the source-removal path). From here
   the source is searchable.
4. **`tag`**. Tag the live chunks as `_tag_chunks` does now. `kc_tagging.tag_chunk` lets
   `CallRefused` propagate (a parse failure is still skipped as today). On success,
   `stage = NULL`. On a refusal or other exception, the source stays `done` with
   `stage = "tag"` and the error is logged; the reconcile sweep retries it.

Nothing reads `staged_chunks` except the embed and publish stages, so retrieval (and every
query that filters `chunks.superseded_at IS NULL`) never sees a half-ingested source; a source
being re-processed keeps answering with its old chunks until publish.

### Claims and slots

- `ingest_source` first takes two advisory locks on a dedicated connection held until the job
  ends (the `turn_lock` pattern): a global slot `pg_try_advisory_lock(<ns>, i)` for some
  `i < ingest_max_concurrent_jobs` (4), and a learner slot
  `pg_try_advisory_lock(<ns2>, hashtext(learner_id) * 16 + j)`-style key for some
  `j < ingest_max_jobs_per_learner` (new, default 2). If either is unavailable the job does
  nothing (returns `None`), leaving the source `pending`. A crashed worker's connection ends and
  Postgres frees its slots.
- The existing claim UPDATE keeps its lease, attempts and status conditions; its
  `live_jobs < ingest_max_concurrent_jobs` subquery is removed.
- Claimable: `pending`; `processing` with a lapsed lease; and (new) `done` with
  `stage = "tag"` and `attempts < ingest_max_attempts`.
- A claimed source runs from its `stage`. A tag-stage claim runs only stage 4.
- When a job ends (any outcome) it dispatches the oldest `pending` source that may now get a
  slot — preferring the same learner's — so queued uploads do not wait for the reconcile sweep.
- The reconcile sweep also re-enqueues `done` sources with `stage = "tag"`, attempts left, and
  `updated_at` older than `ingest_reconcile_grace_seconds`. Exhausted tag retries are not
  parked as failed: the source stays `done`, untagged, logged once
  (`ingest.tagging_abandoned source=…`).

### Failure and retry

- A failure mid-stage discards only that transaction's uncommitted writes; earlier stages'
  commits and completed embed batches stay. Transient vs terminal is `_is_terminal`, unchanged.
- Re-processing a finished source (S50, `reset_for_reingest`) clears `stage`, deletes its
  staged rows and its artifact, then starts at `extract`.
- `ingest_job_timeout_seconds` and `ingest_max_attempts` apply per claim as now.

### Removal and erasure

Deleting a source and erasing an account (V12) remove the source's staged rows (foreign key,
`ON DELETE CASCADE`) and its artifact key; an artifact delete the store refuses becomes a
`pending_erasures` row, like the source's own blob.

## Data

Migration `0076_resumable_ingestion`:

- `sources.stage` Text NULL (existing rows NULL).
- `staged_chunks`: `id` UUID PK, `source_id` FK → `sources.id` ON DELETE CASCADE (indexed),
  `ordinal` int, `text` Text, `embedding` vector(`embed_dim`), `embedding_space` Text,
  `pipeline_version` Text, `provenance` JSONB, `created_at`; unique (`source_id`, `ordinal`).

Setting: `ingest_max_jobs_per_learner: int = Field(default=2, gt=0)`, uncalibrated (S18).

## Testing

- Resume: a counting fake adapter plus an embed refused on the second batch — the retry does not
  extract again, embeds only the missing ordinals, publishes the right count. Same after a
  lapsed lease reclaimed by a second job.
- Invisible until publish: staged chunks are not retrieved; a re-processed source answers with
  its old chunks until publish.
- Publish supersedes old chunks (cited kept), removes staged rows and the artifact.
- Tagging: a refused tag leaves `done` / `stage = "tag"` with searchable chunks; reconcile
  re-enqueues; the retry tags without re-embedding; exhausted → `done`, untagged.
- Slots: four global slots held → fifth claim `None`; two learner slots held → that learner's
  third `None`, another learner's succeeds; a closed connection frees its slot; a finishing job
  dispatches the next pending source.
- Erasure: deleting a source or an account removes staged rows and the artifact.
- Migration 0076 data test; `db-check` clean.

## Docs

Tracker: S37 → Completed (per-job spend ceiling recorded as deferred; the S49 tagging note
resolved). RUNBOOK §5 (Ingesting content): stages, slots, per-learner cap, tag retry,
artifacts. CLAUDE.md: one line. S18: `ingest_max_jobs_per_learner`.

## Out of scope

A per-job spend ceiling; a deployment-wide model-call ceiling; one queued job per stage;
resuming inside extraction (page-by-page OCR) — a huge scanned PDF still extracts in one pass,
bounded by the job timeout.
