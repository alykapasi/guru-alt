# Guru

**An AI-first personalized learning platform** whose promise is *durable* learning — knowledge that
sticks. Guru adapts content, pacing, scaffolding, and assessment to each learner, with a hierarchical
knowledge graph, a continuous mastery model, and an evidence-based learner profile at its core.

> **Design docs (read these to understand the project):**
> [docs/MASTERPLAN.md](docs/MASTERPLAN.md) (vision + architecture) ·
> [docs/ROADMAP.md](docs/ROADMAP.md) (phased plan + what's landed) ·
> [docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md) (engineering detail) ·
> [docs/RUNBOOK.md](docs/RUNBOOK.md) (how to operate it).

Note: "alt" is a version suffix for the current build, not part of the product name.

---

## Status

**Phases 0–8 are complete; Phase 9 (experiment & evaluation suite) is in progress.** See
[docs/ROADMAP.md](docs/ROADMAP.md) for the per-phase landed-notes.

| Area | State |
| ---- | ----- |
| Domain core + knowledge graph (Subject → Topic → KC) | ✅ Phase 1 |
| LLM abstraction + model-role registry + tutor chat | ✅ Phase 2 |
| Knowledge tracer (continuous Elo/IRT) + assessment + FSRS | ✅ Phase 3 |
| Multimodal ingestion (docs/OCR/ASR/web) + RAG | ✅ Phase 4 |
| Orchestration, lesson-plan policy, placement, profile, memory | ✅ Phase 5 |
| Agentic tools + workflow mode | ✅ Phase 6 |
| Frontend MVP (React + Vite) | ✅ Phase 7 |
| Notes (durable learning artifact) | ✅ Phase 8 |
| Eval sweeps + MLflow (9a) · real-data datasets (9b) · DSPy optimization (9c) | 🚧 Phase 9 |
| Auth + admin portal · hardening | ☐ Phases 10–11 |

**Auth is deliberately stubbed** behind a seam (`learner_id` is threaded everywhere already) until
Phase 10 — this is not yet a multi-tenant system.

---

## What's built

- **Learning engine** — a hierarchical knowledge graph; a continuous **Elo/IRT knowledge tracer**
  with per-KC ability + uncertainty rolled up into subject scores; **FSRS** scheduling for retention;
  LLM rubric grading for partial credit; an adaptive lesson-plan policy; and a behavior-derived
  **learner profile** (how they learn, not VARK).
- **Tutoring** in three composable modes — chat, agentic (tool-calling), and a fixed guided-practice
  workflow — all as LangGraph graphs.
- **Multimodal ingestion** — documents, vision-LLM OCR, Whisper ASR (audio/video), and web links →
  normalize → chunk → embed → store with provenance, then hybrid retrieval (vector + full-text +
  metadata). Per-chunk KC auto-tagging scopes chunks to the graph.
- **Notes** — per-learner, per-topic artifacts that grow as the learner studies, with format
  projections (outline / narrative / mnemonic / worked-examples) and full revision history.
- **Evaluation suite** — a golden/live eval harness, a config-sweep + ablation runner with MLflow
  tracking, real-data dataset mining, and DSPy prompt compilation with measured deltas.
- **52 API endpoints** across 12 routers, and a React frontend covering chat, lessons/sessions,
  dashboard, uploads, and notes.

---

## Architecture

Layered FastAPI backend. The load-bearing rule: **application code references LLMs by *role*, never
by model name, and never calls a provider SDK directly.**

```
API routers  →  services  →  { learning engine · agent graphs · rag · memory }  →  llm registry  →  provider
```

Key seams (each swappable without touching callers):

| Seam | What it hides |
| ---- | ------------- |
| `app/llm/` **model-role registry** | `FAST` / `SMART` / `GENIUS` / `VISION` / `EMBED` → `(provider, model)` per env. Providers: Ollama (dev), OpenRouter / Anthropic (prod), `FakeProvider` (tests). Every call is token/cost logged, tagged by role + model. |
| `app/learning/` **`KnowledgeTracer`** | The mastery estimator. Continuous Elo/IRT today; the KC-tagged event log earns a DKT upgrade later without a rewrite. |
| `app/prompts/` **`RoleLM`** | The *only* bridge from DSPy to the role registry — DSPy never reaches a provider or litellm. |
| `app/rag/` **`Transcriber` / `Demuxer`** | ASR and video demux, so CI runs offline against fakes. |
| `app/storage/` **blob store** | S3-compatible object storage (MinIO in dev). |
| `app/workers/` **taskiq broker** | Redis queue in dev/prod, in-memory for tests. |
| `get_current_learner` | Stubbed auth — swapped for a real resolver in Phase 10. |

---

## Tech Stack

- **Backend:** Python 3.13 · FastAPI · Uvicorn · Pydantic v2 · async SQLAlchemy 2.0 · taskiq (Redis)
- **Database:** PostgreSQL 17 + pgvector (HNSW) + pg_trgm/GIN, via Alembic migrations
- **AI / LLM:** provider-agnostic layer addressed **by role** (Ollama dev · OpenRouter/Anthropic
  prod) · LangGraph orchestration · DSPy prompt optimization · FSRS scheduling · MLflow eval tracking
- **Storage:** S3-compatible object storage (MinIO in dev) · faster-whisper ASR (optional extra) ·
  PyMuPDF / python-docx / python-pptx / trafilatura for ingestion
- **Tooling:** [uv](https://docs.astral.sh/uv/) (packaging) · ruff (lint/format) · ty (types) ·
  beartype (runtime types) · pytest · poethepoet (task runner) · pre-commit
- **Frontend:** React 19 · TypeScript · Vite · React Router

---

## Prerequisites

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Docker + Docker Compose** — Postgres, Redis, MinIO
- **[Ollama](https://ollama.com/)** — the dev default for every model role (chat works offline)
- *Optional:* **ffmpeg/ffprobe** on `PATH` for video ingestion · the `asr` extra for audio
  transcription (`uv sync --extra asr`)

---

## Quickstart

```bash
# 1. Install dependencies (creates the .venv automatically)
uv sync

# 2. Create your local env file
cp .env.example .env

# 3. Start Postgres (pgvector) + Redis + MinIO
docker compose up -d

# 4. Apply database migrations (enables vector + pg_trgm, etc.)
uv run poe db-upgrade

# 5. Pull the dev models Ollama serves for each role
ollama pull llama3.2 && ollama pull llama3.2-vision && ollama pull nomic-embed-text

# 6. Run the dev server
uv run poe dev

# 7. (Recommended) install git pre-commit hooks
uv run poe hooks-install
```

The API is now at <http://localhost:8000> — health check at
[`/health`](http://localhost:8000/health), interactive OpenAPI docs at
[`/docs`](http://localhost:8000/docs).

Background jobs (ingestion, memory write-back) need a worker in a second terminal:

```bash
uv run poe worker
```

And the frontend in a third:

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

> **Note on ports:** Postgres is published on host port **5433** (not the default 5432) to avoid
> clashing with a local Postgres. This matches `GURU_DATABASE_URL` in `.env.example`.

For day-to-day operations — ingesting a document, running a sweep, compiling a prompt, and the
common failure modes — see **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

---

## Commands

All backend tasks run through `poethepoet`. List them with `uv run poe --help`.

**Develop**

| Command | Description |
| ------- | ----------- |
| `uv run poe dev` | Start the FastAPI dev server (auto-reload) |
| `uv run poe worker` | Start the taskiq worker (ingestion + memory jobs) |
| `uv run poe db-upgrade` | Apply migrations (`alembic upgrade head`) |
| `uv run poe db-downgrade` | Roll back one migration |

**Verify**

| Command | Description |
| ------- | ----------- |
| `uv run poe check` | **The green gate:** lint + type-check + test |
| `uv run poe test` | Run the test suite |
| `uv run poe test-watch` | Run tests fail-fast in watch mode |
| `uv run poe lint` | Lint with ruff |
| `uv run poe format` | Auto-format with ruff |
| `uv run poe format-check` | Check formatting without modifying |
| `uv run poe type-check` | Type-check with ty |
| `uv run poe hooks-install` | Install the git pre-commit hooks |
| `uv run poe hooks` | Run all pre-commit hooks against the whole repo |

**Evaluate**

| Command | Description |
| ------- | ----------- |
| `uv run poe eval` | Deterministic offline eval (grading + tracer suites) — no model, free |
| `uv run poe sweep <config.yaml>` | ⚠️ Run a prompt × model × config sweep, logged to MLflow |
| `uv run poe sweep-report` | Leaderboard + pairwise ablation diff for a sweep |
| `uv run poe build-calibration-dataset` | Mine the event log into a tracer-calibration dataset (needs a live DB; no model) |
| `uv run poe compile-prompt kc_tagging` | ⚠️ BootstrapFewShot-compile a DSPy module |
| `uv run poe prompt-report kc_tagging` | ⚠️ Baseline-vs-compiled delta on the held-out dev split |

⚠️ = calls real models (costs money). None of these are part of `poe check`.

**Frontend** (from `frontend/`)

| Command | Description |
| ------- | ----------- |
| `npm run dev` | Vite dev server |
| `npm run build` | Type-check + production build |
| `npm run lint` | ESLint + Prettier check |
| `npm run gen:api` | Regenerate API types from the running backend's OpenAPI schema |

---

## Configuration

All settings are environment variables prefixed `GURU_` (see [`.env.example`](.env.example)), loaded
via pydantic-settings into `app/core/config.py`. Every field has a working dev default.

**Core**

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `GURU_ENV` | `dev` | `dev` \| `test` \| `prod` (controls log style + runtime type-checking) |
| `GURU_DATABASE_URL` | `postgresql+asyncpg://guru:guru@localhost:5433/guru` | Async Postgres URL |
| `GURU_REDIS_URL` | `redis://localhost:6379/0` | Redis (job queue) |
| `GURU_LOG_LEVEL` / `GURU_LOG_JSON` | `INFO` / `false` | Log level; `true` for JSON logs (prod) |
| `GURU_CORS_ORIGINS` | `["http://localhost:5173"]` | Browser origins allowed to call the API |

**Models** — each role is a `provider:model` string. Providers: `ollama`, `openrouter`, `anthropic`.

| Variable | Dev default | Notes |
| -------- | ----------- | ----- |
| `GURU_MODEL_FAST` | `ollama:llama3.2` | High-volume/cheap work (KC tagging, classification) |
| `GURU_MODEL_SMART` | `ollama:llama3.2` | Default tutoring, grading, notes |
| `GURU_MODEL_GENIUS` | `ollama:llama3.2` | Hardest reasoning |
| `GURU_MODEL_VISION` | `ollama:llama3.2-vision` | **Must be multimodal** (OCR of scanned pages/frames) |
| `GURU_MODEL_EMBED` | `ollama:nomic-embed-text` | Embeddings; `GURU_EMBED_DIM` (768) must match |
| `GURU_OPENROUTER_API_KEY` / `GURU_ANTHROPIC_API_KEY` | *(empty)* | Required only for cloud providers |

> Changing the EMBED model's output dimension is a **schema migration** (the pgvector column is
> fixed-width) — see the runbook.

**Object storage** (`GURU_BLOB_*`) defaults to the MinIO compose service on `localhost:9000`.
Ingestion, ASR, and tuning knobs (concurrency, batch sizes, confidence floors, round caps) are all
in `app/core/config.py` with inline rationale.

`.env` is git-ignored. Outside `prod`, beartype runtime type-checking is active across the `app`
package.

---

## Testing & evaluation

```bash
uv run poe check         # lint + type-check + test — what CI runs, and the merge gate
uv run poe test          # tests only
```

The suite runs **fully offline and deterministically**: LLM calls go through a `FakeProvider`, ASR
and video demux through fakes, and the job broker in-memory. Integration tests use the Postgres
started by `docker compose`.

Beyond the pass/fail suite, `tests/eval/` holds the measurement layer — a golden/live **harness**,
the config **sweep** runner + MLflow tracking, real-data **datasets** mined from the event log, and
DSPy **prompts** compilation. `poe eval` is deterministic and free; the sweep and DSPy compile/report
call real models, so they stay manual and out of the gate. The runbook walks through each.

---

## Quality & CI

- **Pre-commit hooks** ([`.pre-commit-config.yaml`](.pre-commit-config.yaml)) run ruff (lint+format)
  and ty on staged files, plus basic hygiene checks. Install with `uv run poe hooks-install`; run
  everything with `uv run poe hooks`. They use the project's pinned tool versions via `uv run`.
- **CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs format-check, lint,
  type-check, tests, and a migration-apply check against a pgvector service on every push to `main`
  and every PR.

---

## Project Structure

```text
guru-alt/
├── app/
│   ├── __init__.py        # enables beartype runtime checks (dev/test)
│   ├── main.py            # FastAPI app: lifespan, middleware, /health
│   ├── api/v1/            # 12 routers, 52 endpoints
│   ├── core/              # config, db (engine/session/Base), logging, middleware
│   ├── models/            # SQLAlchemy ORM models
│   ├── schemas/           # Pydantic boundary schemas
│   ├── services/          # business logic behind the routers
│   ├── llm/               # provider-agnostic LLM + model-role registry
│   ├── prompts/           # DSPy programs + RoleLM seam + compiled artifacts
│   ├── agent/             # LangGraph graphs (tutor turn · refinement · workflow)
│   ├── rag/               # multimodal ingestion + hybrid retrieval
│   ├── learning/          # knowledge graph · tracer · FSRS · profile · lesson policy · notes
│   ├── memory/            # persistent per-learner memory
│   ├── storage/           # S3-compatible blob store
│   └── workers/           # taskiq broker + job wrappers
├── frontend/              # React 19 + TypeScript + Vite app
├── db/migrations/         # Alembic (async); 0001 enables vector + pg_trgm
├── tests/
│   └── eval/              # harness · sweep runner · real-data datasets · DSPy compile/report
├── docs/                  # MASTERPLAN · ROADMAP · TECHNICAL_DESIGN · RUNBOOK
├── docker-compose.yml     # Postgres (pgvector) + Redis + MinIO
├── pyproject.toml         # deps + poe tasks + tool config
├── .pre-commit-config.yaml
└── CLAUDE.md              # guidance for Claude Code
```

---

## Development Philosophy

Pragmatic Programmer principles (DRY, YAGNI), conciseness and performance, plan-before-build with
small verified increments, async-first, and type safety via Pydantic + beartype + ty. Heavy
dependencies enter at the phase that needs them, behind thin seams. Every change should leave
`uv run poe check` green.
