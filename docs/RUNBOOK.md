# Guru — Runbook

Operational procedures for running, exercising, and debugging Guru.

> **Scope.** Guru has **no production deployment yet** — auth is stubbed until Phase 10 and
> hardening is Phase 11 (see [ROADMAP.md](ROADMAP.md)). This runbook therefore covers **local
> development and the evaluation/experiment suite**, which is where all operational work happens
> today. It is not a production incident runbook; there is no production to page for.

**Related:** [README.md](../README.md) (setup + commands) ·
[MASTERPLAN.md](MASTERPLAN.md) (why) · [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md) (how) ·
[ROADMAP.md](ROADMAP.md) (what's landed).

---

## 1. Start and stop the stack

Guru needs four things running for full functionality: **infrastructure** (Postgres/Redis/MinIO),
the **API**, a **worker**, and optionally the **frontend**.

```bash
# Infrastructure — Postgres (5433), Redis (6379), MinIO (9000/9001)
docker compose up -d
docker compose ps                      # all services should be "running"/healthy

# API (terminal 1)
uv run poe dev                         # http://localhost:8000

# Worker (terminal 2) — REQUIRED for ingestion and memory write-back
uv run poe worker

# Frontend (terminal 3)
cd frontend && npm run dev             # http://localhost:5173
```

**Verify each layer:**

```bash
curl -s localhost:8000/health                      # API  → {"status":"ok"}
docker compose exec postgres pg_isready -U guru    # DB   → accepting connections
docker compose exec redis redis-cli ping           # Redis→ PONG
curl -s localhost:9000/minio/health/live -o /dev/null -w '%{http_code}\n'   # MinIO → 200
curl -s localhost:11434/api/tags | head -c 200     # Ollama → JSON list of pulled models
```

**Stop:**

```bash
docker compose down          # stop services, keep data volumes
docker compose down -v       # stop AND DELETE all data (Postgres, Redis, MinIO)
```

---

## 2. Who am I acting as? (stubbed auth)

There is **no login**. `get_current_learner` ([app/api/deps.py](../app/api/deps.py)) resolves every
request to a single learner with handle `dev`, **created automatically on first use**. All API calls
share that learner's data.

This means: any manual API poking accumulates rows under the `dev` learner. See
[§8 Cleanup](#8-data-hygiene--cleanup) before you go hunting for "why does this learner already have
20 conversations".

Real auth (and multi-tenancy) arrives in Phase 10; `learner_id` is already threaded everywhere
behind this seam, so it is a swap, not a rewire.

---

## 3. Database operations

```bash
uv run poe db-upgrade                 # apply all migrations (alembic upgrade head)
uv run poe db-downgrade               # roll back exactly one migration

# Create a new migration after changing app/models/
uv run alembic revision --autogenerate -m "short description"
# ALWAYS read the generated file before committing — autogenerate misses
# server defaults, enum changes, and index/constraint renames.

uv run alembic current                # which revision is applied
uv run alembic history --verbose      # full migration history
uv run poe db-check                   # fail if a model has drifted from the migrations
```

`db-check` is `alembic check`: it autogenerates against the live database and fails if that
would produce any operation — i.e. someone changed `app/models/` without writing the migration.
CI runs it, so a drifted model is caught before it reaches anyone else's database.

**Open a psql shell:**

```bash
docker compose exec postgres psql -U guru -d guru
```

**Nuke and rebuild the database** (destroys all local data):

```bash
docker compose down -v && docker compose up -d
sleep 5 && uv run poe db-upgrade
```

> Migration `0001` enables the `vector` and `pg_trgm` extensions. If you point Guru at a fresh
> Postgres that is **not** the pgvector image, `db-upgrade` fails at extension creation.

---

## 4. Models and providers

Code never names a model. It asks for a **role**; the registry resolves the role to
`provider:model` from settings ([app/core/config.py](../app/core/config.py)).

| Role | Used for | Dev default |
| ---- | -------- | ----------- |
| `FAST` | High-volume cheap work (KC tagging, classification) | `ollama:llama3.2` |
| `SMART` | Default tutoring, grading, notes | `ollama:llama3.2` |
| `GENIUS` | Hardest reasoning | `ollama:llama3.2` |
| `VISION` | OCR of scanned pages / video frames — **must be multimodal** | `ollama:llama3.2-vision` |
| `EMBED` | Embeddings — dimension must match `GURU_EMBED_DIM` | `ollama:nomic-embed-text` |

**Run fully local (default):**

```bash
ollama serve                                    # if not already running as a service
ollama pull llama3.2 && ollama pull llama3.2-vision && ollama pull nomic-embed-text
```

**Point a role at a cloud model** — edit `.env`:

```bash
GURU_MODEL_SMART=openrouter:anthropic/claude-sonnet-4-6
GURU_MODEL_GENIUS=openrouter:anthropic/claude-opus-4-8
GURU_OPENROUTER_API_KEY=sk-or-...
# or: GURU_MODEL_SMART=anthropic:claude-sonnet-4-6 + GURU_ANTHROPIC_API_KEY=sk-ant-...
```

Restart `poe dev` (and `poe worker`) after changing `.env` — settings are cached per process.

> **Two traps.** (1) `VISION` **must** resolve to a multimodal model or scanned-PDF/video ingestion
> fails at the OCR step. (2) Changing `EMBED` to a model with a different output dimension is a
> **schema migration** — the pgvector column is fixed-width. Update `GURU_EMBED_DIM`, write a
> migration, and re-embed existing chunks; otherwise inserts fail on dimension mismatch.

---

## 5. Ingesting content

Ingestion is asynchronous: the endpoint returns `202 Accepted` immediately and a **worker** does the
real work. **If `poe worker` is not running, sources sit unprocessed forever.**

```bash
# Upload a file (subject_id/topic_id optional — they scope KC auto-tagging)
curl -X POST localhost:8000/api/v1/sources/upload \
  -F "file=@/path/to/lecture.pdf" \
  -F "subject_id=<uuid>"

# Register a web page
curl -X POST localhost:8000/api/v1/sources/link \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/article"}'

# Watch it progress
curl -s localhost:8000/api/v1/sources | jq '.[] | {id, origin, status, error}'
curl -s localhost:8000/api/v1/sources/<source_id>/chunks | jq 'length'

# Query the index
curl -X POST localhost:8000/api/v1/retrieve \
  -H 'content-type: application/json' \
  -d '{"query":"what is gradient descent","limit":5}'
```

**Format prerequisites**

| Input | Needs |
| ----- | ----- |
| PDF / DOCX / PPTX / XLSX / EPUB / text | nothing extra |
| Scanned PDF (image pages) | a multimodal `VISION` model |
| Audio | `uv sync --extra asr` (faster-whisper) |
| Video | the `asr` extra **and** `ffmpeg`/`ffprobe` on `PATH` |
| Web link | outbound network |

Tuning knobs (`ocr_concurrency`, `embed_batch_size`, `embed_concurrency`, `kc_tag_concurrency`,
`kc_tag_min_confidence`, `max_upload_bytes`) all live in `app/core/config.py` with inline rationale.
DB writes stay serialized regardless of concurrency settings — only network/CPU work parallelizes.

---

## 6. The evaluation & experiment suite

Four distinct tools. Only the first is free and automatic.

### 6.1 Deterministic eval — free, offline

```bash
uv run poe eval        # grading + tracer suites; no model, no DB. Exits non-zero on any failure.
```

### 6.2 Config sweeps + MLflow (Phase 9a) — ⚠️ calls real models

Define a matrix; every cell runs the harness suites and logs one MLflow run.

```yaml
# tests/eval/experiments/my-sweep.yaml
name: my-sweep
suites: [rubric]                 # which harness suites to run
axes:
  smart:                         # candidate models for the SMART role
    - {provider: openrouter, model: claude-sonnet-4-6}
    - {provider: ollama,     model: llama3.2}
gen_config: [{}]                 # see below — only declared settings are accepted
toggles: {}
```

**A sweep may only vary what it can actually apply.** `gen_config` keys and `toggles` names are
checked against `tests/eval/sweep/settings.py` when the config loads, and an unknown one is
refused before the first paid call. Varying a setting nothing applies would produce two identical
runs reported as a comparison — a wrong answer, not a missing one. Currently declared:

| setting | kind | suite | effect |
| --- | --- | --- | --- |
| `kc_tag_min_confidence` | `gen_config` | `kc_tagging` | confidence a predicted tag must reach to be kept (0.0–1.0) |
| `strict_kc_tagging` | `toggle` | `kc_tagging` | shorthand for `kc_tag_min_confidence: 0.7` |

Nothing tunes the `rubric` or `retrieval` suites yet; adding a knob means declaring it there and
wiring it through `run_cell`. Each run also records every role's resolved `provider:model` (not
only the swept ones), the settings **as applied**, and a digest of the golden case file it scored
against, so a run can be reproduced from its own record.

```bash
uv run poe sweep tests/eval/experiments/my-sweep.yaml
uv run poe sweep-report --experiment my-sweep              # leaderboard (quality desc, cost tie-break)
uv run poe sweep-report --experiment my-sweep --compare <RUN_A> <RUN_B>   # pairwise ablation diff

# Browse runs in the MLflow UI
uv run mlflow ui --backend-store-uri file:./mlruns         # http://localhost:5000
```

A failed cell logs `status=failed` and **never aborts the sweep** — always check the leaderboard for
failed rows before trusting a comparison. Results land in `mlruns/`; per-case report artifacts are
staged in `sweep-artifacts/<name>/`. Both are gitignored.

### 6.3 Real-data datasets (Phase 9b) — needs a live DB, no model

```bash
uv run poe build-calibration-dataset
# → writes tests/eval/datasets/tracer-calibration.json (GITIGNORED — never commit learner data)
```

Mines the `LearningEvent` log into per-`(learner, KC)` sequences and scores the tracer with
prequential (predict-then-update) replay through the *production* estimator. Prints
`0 sequences` if you have not run any sessions yet — that is expected on a fresh DB, not a failure.

### 6.4 DSPy prompt optimization (Phase 9c) — ⚠️ calls real models

```bash
uv run poe compile-prompt kc_tagging    # BootstrapFewShot → app/prompts/artifacts/kc_tagging.json
uv run poe prompt-report kc_tagging     # baseline vs compiled on the held-out dev split
```

`prompt-report` prints `baseline(uncompiled)=… compiled=… delta=±…` and logs two MLflow runs.

**Commit the artifact only if the delta is a win:**

```bash
git add app/prompts/artifacts/kc_tagging.json
git commit -m "chore(9c): commit compiled kc_tagging artifact"
uv run poe check     # confirm the committed artifact loads cleanly
```

If the delta is negative or zero, leave the artifact uncommitted — the runtime keeps using the
uncompiled fallback, which is the safe default.

### 6.5 Retrieval recall + plan (S76) — needs a live DB, no model

```bash
uv run poe retrieval-recall                # 40k synthetic chunks (a few minutes)
uv run poe retrieval-recall --rows 5000    # quicker, less representative
```

Seeds its own throwaway database (`<db>_recall`) and reports three things about the vector arm:
which plan Postgres actually chooses for the scoped query, that plan's recall against the same
query forced onto an exact scan, and what the HNSW index would give instead across
`hnsw.ef_search`. It exists because whether retrieval is exact or approximate is the planner's
decision, not ours, and the two fail in completely different ways.

As of 2026-09-08 the answer is **exact**: the join to `sources` keeps the planner on
`ix_chunks_source_id`, so the HNSW index is never reached and costs about 4 µs per chunk the
learner owns. Recall figures for the index-reachable shape are a *lower bound* — the vectors are
synthetic and near-uniform, which is close to worst case for a graph index. See S76 in the
suggestions tracker for the numbers and what they do and do not license.

---

## 7. Before you merge

```bash
uv run poe check     # lint + type-check + full test suite — the gate. Must be green.
```

The suite runs **fully offline** (FakeProvider for LLMs, fakes for ASR/demux, in-memory broker), so
a green local run means a green CI run.

**Live-model tests are opt-in.** The provider integration test, the eval rubric suite and vision
OCR call a real local model; they skip unless `GURU_LIVE_MODEL_TESTS=1` and Ollama has a matching
model pulled:

```bash
GURU_LIVE_MODEL_TESTS=1 uv run poe test
```

They used to run automatically whenever Ollama happened to be reachable, which is why `poe test`
could take twenty minutes on a laptop and twenty-five seconds in CI, and could fail on a model's
mood rather than on the code. Pre-commit hooks (`uv run poe hooks-install`) catch
format/lint/type issues before they reach a commit.

**Tests run on their own database.** `poe test` first runs `poe test-db-init`, which creates and
migrates `<your database>_test` (derived from `GURU_DATABASE_URL` — same host, same credentials)
and points the suite at it. This is not cosmetic: several tests assert on *global* rows (total
`llm_calls`, event counts) and claim the fixed `dev` learner handle, so a dev server writing to the
same database makes them fail for reasons that have nothing to do with the code. If you see a burst
of unrelated failures, check you aren't overriding `GURU_DATABASE_URL` to a database something else
is using.

CI runs three gates: this suite, `alembic upgrade head` + `db-check` against a fresh pgvector
service, and a separate **frontend** job (`npm ci && npm run lint && npm run build` — `build` is
`tsc -b`, so it is the frontend's type-check too).

> **Check the frontend with `npm run build`, never `npx tsc --noEmit`.** The root `tsconfig.json`
> is solution-style (`"files": []` plus project references), so a bare `tsc --noEmit` type-checks
> *zero* files and exits 0 on code it never read. Only `tsc -b` follows the references.

After changing any response schema, regenerate the frontend's API types, or the client compiles
against a contract the server no longer serves:

```bash
uv run python -c "import json; from app.main import app; json.dump(app.openapi(), open('/tmp/openapi.json','w'))"
npx -y openapi-typescript@7.13.0 /tmp/openapi.json -o frontend/src/api/schema.d.ts
```

`openapi-typescript` is fetched per-run rather than installed: it declares a peer dependency on
TypeScript 5, the frontend is on 6, and `npm ci` refuses the conflict. It generates correct output
against 6 — the constraint is stale, not real — but keeping it out of the dependency graph means
`npm ci` stays strict instead of being run with `--legacy-peer-deps`, which would hide the next
conflict too. The version is pinned at the call site.

---

## 8. Data hygiene & cleanup

**Everything you do manually accumulates under the `dev` learner.** To clear it out:

```sql
-- in: docker compose exec postgres psql -U guru -d guru
DELETE FROM learners WHERE handle = 'dev';
```

> ⚠️ **`llm_calls` rows are NOT deleted with the learner.** That FK is `ON DELETE SET NULL`
> (deliberately — cost history outlives the account), so deleting a learner leaves orphaned
> `llm_calls` rows with `learner_id = NULL`. Clear them explicitly if you want a truly clean slate:
> `DELETE FROM llm_calls WHERE learner_id IS NULL;`

**Gitignored artifacts** (safe to delete anytime, regenerated on demand):

| Path | What |
| ---- | ---- |
| `mlruns/` | MLflow local tracking store |
| `sweep-artifacts/` | Per-case sweep report artifacts |
| `tests/eval/datasets/*.json` | Mined real-data eval sets — **never commit** |
| `.venv/`, `.pytest_cache/`, `.ruff_cache/`, `.ty_cache/` | Tool caches |

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `connection refused` on startup | Postgres on **5433**, not 5432 | Check `GURU_DATABASE_URL`; `docker compose ps` |
| `db-upgrade` fails creating extensions | Not the pgvector image | Use the compose Postgres (`pgvector/pgvector:pg17`) |
| Uploaded source never leaves pending | **No worker running** | Start `uv run poe worker` |
| Upload returns 413 | Exceeds `max_upload_bytes` (1 GiB default) | Raise `GURU_MAX_UPLOAD_BYTES` or split the file |
| Upload fails writing the blob | MinIO down / bucket missing | `docker compose up -d minio`; the `minio-bootstrap` service creates the bucket |
| Any LLM call hangs or errors in dev | Ollama not running, or model not pulled | `ollama serve`; `ollama pull <model>` |
| Scanned PDF ingests with empty text | `VISION` role is not multimodal | Point `GURU_MODEL_VISION` at a vision model |
| Audio ingestion fails | `asr` extra not installed | `uv sync --extra asr` |
| Video ingestion fails | `ffmpeg`/`ffprobe` not on `PATH` | Install ffmpeg, or set `GURU_FFMPEG_BIN`/`GURU_FFPROBE_BIN` |
| Embedding insert fails on dimension | `EMBED` model dim ≠ `GURU_EMBED_DIM` / column | Align settings + write a migration; re-embed |
| `.env` change has no effect | Settings cached per process | Restart `poe dev` **and** `poe worker` |
| KC tagging returns nothing | Confidence floor too high, or no candidate KCs in scope | Lower `GURU_KC_TAG_MIN_CONFIDENCE`; check the source's subject/topic |
| Sweep row shows `status=failed` | That cell's model/config errored | Inspect the run in MLflow UI; sweeps never abort on one failure |
| `sweep-report` shows nothing | Wrong `--experiment` name | Must match `name:` in the sweep YAML |
| Compiled DSPy artifact seems ignored | Artifact corrupt → silent fallback (by design) | Check logs for `kc_tagging.artifact_load_failed`; recompile |
| A `@broker.task`-decorated function loses `.kiq()` | beartype's import hook rewrites the decorated object | Already solved — apply `broker.task(...)` as a **plain assignment**, not decorator syntax (see the comment in [app/workers/tasks.py](../app/workers/tasks.py)) |

**Reading logs.** Dev logs are human-readable console output (structlog). For machine-parseable
logs set `GURU_LOG_JSON=true`. Raise detail with `GURU_LOG_LEVEL=DEBUG`; set `GURU_DB_ECHO=true` to
see every SQL statement.

---

## 10. Cost control

Every LLM call is logged to `llm_calls` with tokens and cost, **tagged by role and model** — this
has been true since Phase 2 and is the substrate the admin portal (Phase 10) will read.

```sql
SELECT role, model, count(*) AS calls,
       sum(input_tokens) AS in_tok, sum(output_tokens) AS out_tok,
       round(sum(cost_usd)::numeric, 4) AS usd
FROM llm_calls GROUP BY role, model ORDER BY calls DESC;
```

Practical guardrails:

- **Dev defaults are entirely local** (Ollama) — you cannot accidentally spend money without editing
  `.env` to point a role at a cloud provider.
- The ⚠️ commands in the README's Evaluate table are the only ones that fan out across many model
  calls. A sweep costs *cells × suite size* calls — check the matrix size before running it.
- Round/iteration caps (`agentic_max_iterations`, `refinement_max_rounds`, `workflow_max_rounds`,
  `reviews_due_item_limit`) exist specifically to bound worst-case calls per request. Raise them
  deliberately.
