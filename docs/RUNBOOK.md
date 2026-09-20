# Guru — Runbook

Operational procedures for running, exercising, and debugging Guru.

> **Scope.** This runbook covers **local development and the evaluation/experiment suite**.
> Guru still has no production deployment — auth is stubbed until Phase 10 and hardening is
> Phase 11 (see [ROADMAP.md](ROADMAP.md)) — but the release, readiness, backup and recovery
> path now exists and lives in **[OPERATIONS.md](OPERATIONS.md)**. Read that one for deploying,
> what to alert on, rolling back, and restoring a backup.

**Related:** [OPERATIONS.md](OPERATIONS.md) (deploy, monitor, recover) ·
[README.md](../README.md) (setup + commands) ·
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

## 2. Who am I acting as?

There **is** a login, and Clerk owns it — see [§11 Identity](#11-identity-s21) for the whole
picture. For everyday local work the short version is:

- With no `GURU_CLERK_SECRET_KEY` set, the app builds and runs with no sign-in panel at all.
  `POST /api/v1/auth/dev-login` issues a session for the `dev` learner with **no credential**,
  and the sign-in page shows a "Development sign-in" button in dev builds. This is the mode
  `poe dev`, the test suite and the browser journeys all run in.
- `GURU_DEV_AUTO_LOGIN=true` is what keeps that endpoint alive. Production refuses to *start*
  while it is on ([app/core/release.py](../app/core/release.py)).
- Anything you poke at manually accumulates rows under the `dev` learner. See
  [§8 Cleanup](#8-data-hygiene--cleanup) before wondering why that learner has 20 conversations.

Posting an address to dev-login (`{"email": "you@example.com"}`) signs you in as that account
instead, creating it if needed — which is how the Playwright journeys get a fresh account per
run now that there is no registration form to drive.

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
| Web link | disabled in v0; upload a local file instead |

`POST /sources/link` remains as a deprecated compatibility endpoint returning 403. URL retries also
return 403. Legacy queued URL jobs fail terminally without fetching or extracting content, retaining
their existing blobs and chunks. Stored sources and citations remain readable; the tutor only
searches stored material. Markdown images cannot automatically request external content.

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

### 6.6 Stranded ingestion jobs (S36/S37)

A source row is committed before its ingestion job is enqueued — they cannot be one
transaction, because Redis is not in the database. So a queue outage between the two leaves a
source nobody will ever process, and a worker that dies mid-job leaves one marked PROCESSING
that nobody is working on.

The worker sweeps for both every `ingest_reconcile_interval_seconds` (default 120). To force a
sweep — after the worker has been down long enough to build a backlog, or to see what one
would collect:

```bash
uv run poe reconcile-ingestion     # prints requeued=N abandoned=N
```

Re-enqueueing is always safe: the *claim* decides who actually runs the job, so a duplicate
delivery finds nothing to take. What the sweep cannot fix is a source that has burned through
`ingest_max_attempts` — those are parked as FAILED with their last real error, and a learner
retries one deliberately:

```bash
curl -X POST localhost:8000/api/v1/sources/<id>/retry     # 409 while a job holds the claim
```

If sources are being abandoned in numbers, the useful question is *which* error they carry:
a terminal one (unsupported type, robots-blocked, over the per-job budget) is working as
intended, while a transient one repeated three times means the provider or store is unwell.

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

## Alpha administration and audited sudo

The admin portal supports broad account visits by authenticated administrators. Enable
`GURU_IMPERSONATION_ENABLED=true` explicitly; the default is false and acts as an operational kill
switch. Learner opt-in is not required. A visit requires a reason and expires after
`GURU_IMPERSONATION_TTL_MINUTES` (default: 15). The administrator's own session stays separate from
the borrowed account credential. Authentication checks the current switch, administrator role,
account existence, and session validity, so disabling the capability, removing the role, or deleting
the administrator prevents further use.

`POST /api/v1/admin/impersonate` starts a visit. Administrators can inspect visit history at
`GET /api/v1/admin/impersonations`, end one with
`DELETE /api/v1/admin/impersonations/{impersonation_id}`, and inspect its actions at
`GET /api/v1/admin/impersonations/{impersonation_id}/actions`. History and ending visits remain
available when the capability is disabled.

Each visited-account action records durable intent before the endpoint acts: method, route template,
path resource IDs, creation time, and its visit's actual actor/effective learner/reason. A completed
response records status and completion time. Endpoint rollback, refusals, and server failures do not
erase the intent. An interrupted response stream remains pending for inspection, including a stream
whose middleware emitted a terminal body before raising. Pending intent proves an action started;
it does not establish that every possible mutation completed. Administrator deletion revokes issued
borrowed sessions through cascading session references while preserving historical audit snapshots.

Admin practice and detours use distinct events; admin placement does not seed learner priors.
None changes the learner's measured ability, uncertainty, or FSRS retention schedule. Admin chat messages and replies show **Admin** and
**Reply to admin**, retain actor/action UUID snapshots without foreign keys, and are excluded from
inferred learner profile and memory extraction. Earlier transcripts retain null attribution; no
historical authorship is inferred or backfilled. These controls implement the alpha sudo slice;
remaining private ownership, release hardening, and operational work still apply.

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

---

## 11. Identity (S21)

Clerk owns credentials — passwords, recovery mail, verification, and the social providers. Guru
holds none of it. Guru keeps **authorization**: who may enroll at all, who is an administrator,
whose account is suspended, and who looked at whose data.

The split matters when something goes wrong, because it decides who you go and fix:

| Symptom | Whose problem |
| --- | --- |
| "I can't reset my password" | Clerk's. Send them to the sign-in panel's own recovery flow. |
| "It says Guru is invite-only" | Yours. They have no open invitation — see the bootstrap below. |
| "It says my account is suspended" | Yours. Reinstate from the portal. |
| Every sign-in answers 503 | Yours. Clerk is unreachable or `GURU_CLERK_SECRET_KEY` is wrong. |
| Every sign-in answers 401 | Clerk's token is being rejected. Check `GURU_CLERK_JWT_KEY` and the authorized parties. |

A 503 and a 401 are deliberately different answers. A 503 means Guru could not *ask* Clerk about
somebody; a 401 means Clerk answered and the answer was no. If an outage ever starts reporting as
401 you will see every learner signed out at once with nothing to alert on, which is precisely the
failure the split exists to prevent.

### Provisioning a Clerk application

From `frontend/`:

```bash
npx -y clerk@latest init          # creates an application and writes a publishable key
npx -y clerk@latest auth login    # claim it against your own Clerk account
```

`init` produces **temporary keys**. They are fine for trying it out and are not production
credentials — claim the application before you depend on it, or you will lose it.

Then in the Clerk dashboard:

- **Sign-up mode: Restricted.** Guru refuses anyone without an open invitation regardless, but
  leaving Clerk's own sign-up open means strangers can create Clerk users that Guru will then
  turn away — confusing for them and noise for you.
- **Email as a required identifier.** Guru links a Clerk user to a learner by verified address.
  A Clerk user with no verified email cannot be matched to anybody.
- **Social connections (three, which is the free-tier cap): Google, Meta, X.** Development
  instances use Clerk's shared OAuth credentials and work immediately; **production needs your
  own OAuth application per provider**, configured in that provider's console. Apple is
  deliberately excluded: Sign in with Apple requires a paid Apple Developer Program membership
  ($99/year), and it would have been a fourth connection against a cap of three.

### Settings

| Setting | Where the value comes from |
| --- | --- |
| `GURU_CLERK_SECRET_KEY` | Clerk dashboard → API keys. Without it nobody can sign in and production refuses to start. |
| `GURU_CLERK_JWT_KEY` | Clerk dashboard → API keys → JWKS public key, PEM-encoded. With it, token verification touches no network; without it every sign-in is a round trip and a Clerk blip becomes your outage. |
| `GURU_CLERK_AUTHORIZED_PARTIES` | Your app's origins. Checked against the token's `azp` claim. Empty falls back to `GURU_CORS_ORIGINS`; with neither set there is no allowlist at all. |
| `GURU_CLERK_SIGN_UP_URL` | Your frontend's `/sign-up` route — where an invitation link lands. |
| `VITE_CLERK_PUBLISHABLE_KEY` | Clerk dashboard → API keys. Goes in `frontend/.env`. Safe to expose. |

The **secret key never reaches the browser**, and the publishable key is the only Clerk value that
belongs in frontend configuration. Leave the publishable key unset and the frontend builds
perfectly well with no sign-in panel — that is the mode CI, vitest and the browser journeys run in,
and it is a supported state rather than a broken one.

### Bootstrapping an empty deployment

Nobody can invite anybody until there is an administrator, and there is no administrator until
somebody has signed in. So the first invitation is issued from the command line, which is the only
authority that exists before a session does:

```bash
uv run poe invite someone@example.com     # --no-send records it without asking Clerk to deliver
# they sign up through Clerk and land in Guru
uv run poe grant-admin someone@example.com
```

From there that person invites everyone else from the portal's **Who may join** panel.

### Importing existing accounts

If your deployment predates Clerk, its learners have passwords that Guru no longer stores — they
were moved to `legacy_password_digests` by migration `0055`. Clerk accepts an Argon2 digest at
import, so those people keep the password they already have instead of being told to reset one
they never chose to lose.

```bash
uv run poe identity-import            # dry run: prints one line per learner, changes nothing
uv run poe identity-import --apply
```

Read the dry run before applying it. Every learner gets one of three outcomes:

- **create** — Clerk has never seen the address. Imported with the digest if there is one.
- **link** — Clerk already has exactly one user with that address. Linked, not duplicated.
- **skip** — with the reason printed. Two Clerk users share the address (a question only you can
  answer: picking one would hand somebody another person's account), or the learner has no
  address at all, which is normal for the `dev` learner.

Each learner is its own transaction, so a Clerk failure halfway through leaves everyone already
imported linked and committed. **Re-run it** — it resumes rather than starting over, and a second
run over finished work changes nothing.

The holding table empties itself as each digest is accepted. An empty `legacy_password_digests` is
the finished state, not a missing step. Anything left in it after a clean `--apply` belongs to a
learner the run skipped, and the skip reason says why.

### What Guru still enforces itself

Clerk proving who somebody is has never been the same as Guru letting them in.

- **Invitations.** `POST /auth/exchange` refuses anybody without an open invitation, so a valid
  Clerk token is not by itself permission to use Guru. Issued and revoked from the portal or
  `poe invite`; both ends are recorded in `account_actions`.
- **Suspension.** Clerk's free tier has no account ban, so Guru implements it. Suspending ends the
  account's live sessions immediately and refuses new ones — it bites a session that is already
  open, not just the next sign-in. Requires a reason, which goes in the audit record.
- **Audited visits.** Unchanged from §"Alpha administration and audited sudo" above. Clerk's own
  impersonation is capped at 5/month on the free plan, which is why Guru keeps its own.

### Open operating decisions

Two things are deliberately unresolved rather than overlooked:

- **Administrators have no MFA.** Multi-factor authentication is a Clerk paid-plan feature
  (Pro, $25/month). Until then an administrator account is protected by one factor, and the
  audited-visit log is what stands behind it.
- **Production needs a domain.** Clerk's development instances are not production-ready, and the
  social connections need your own OAuth applications before they will work outside development.

---

## 12. Publication review (S25b)

Learners keep their curricula private by default. Sharing one is a request, a human review, and
an immutable copy — never a switch that makes the original public.

### What you are actually deciding

Approving puts a **copy** of the subject into the shared library, where every learner can see it.
It does not touch the author's subject, which stays theirs and stays private. Three things follow
from that and are worth holding in mind while you review:

- **What you see is what ships.** The queue shows the snapshot frozen when the author asked, not
  their subject as it is now. If they have rewritten it since, approving still ships what is on
  your screen. That is deliberate — it is the only way the review means anything.
- **The copy is immutable and anonymous.** Learners are not told who wrote it. The author is
  recorded on the publication for your audit, not for display.
- **Approval cannot be undone, only superseded or withdrawn.** Both unlist; neither deletes, and
  neither removes it from anyone already studying it.

Read the answer keys. They are shown because they are part of what you are approving, and a
plausible-looking question with a wrong key is the failure this review exists to catch.

### Working the queue

The queue lives in the admin portal, under **Waiting for review**, oldest first.

- **Approve and share** — optionally untick individual questions first. Unticked ones are left
  behind and the exclusion is recorded on the publication.
- **Reject** — needs a note. It is what the author sees, and a refusal they cannot act on is one
  they will send again unchanged.
- **Withdraw** (on a published subject) — needs a reason. Unlists it; see below.

### What withdrawal and superseding do, exactly

| State | In the catalog | Reachable by id | For learners with a plan on it |
| --- | --- | --- | --- |
| Published | yes | yes | yes |
| Superseded by a newer version | no | **yes** | **yes, still listed** |
| Withdrawn | no | **yes** | **yes, still listed** |

Reaching an unlisted subject by id is not a leak: it was reviewed as shareable. Removing access
instead would break every lesson plan pointing at it, which is why unlisting is where this stops.
**If a publication turns out to contain something that must not be readable at all, withdrawal is
not the tool** — that is a data-removal job against the copy's rows, and the publication record
names them.

### What learners cannot publish, and why

A subject built from the learner's own uploads can never be published (V03). The flag is set the
moment source material reaches the graph, is never cleared, and there is no override — not in the
portal, not in a request body.

The flag is set from what the server observed, not from anything the browser claims:

- the curriculum generation actually retrieved excerpts from their sources;
- sources were moved into the subject when it was committed;
- a source was uploaded scoped to the subject or one of its topics.

**What the flag does not stop, and where you come in.** An author who deliberately requests a
clean curriculum and then pastes source-derived material in as their own edits evades it. Closing
that would mean the server committing only what it generated, which removes the learner's chance
to edit — the wrong trade. So the flag guards against publishing private material *by accident*,
and your review is the control that does not depend on the author's cooperation. If a submission
looks like it was transcribed out of a textbook, it probably was.

### Where the records are

- `publications` — one row per request: the frozen snapshot, who asked, who decided, the notes,
  the excluded items, and the subject that was created.
- `subjects.publication_id` — on a published copy, the decision that created it.
- `subjects.superseded_by_id`, `withdrawn_at`, `withdrawn_reason` — its later life.

The foreign keys are `SET NULL` and the handles are kept as text, so the record survives the
author closing their account. Closing an account clears the author's half and keeps the
reviewer's: an audit any author can erase is not an audit.
