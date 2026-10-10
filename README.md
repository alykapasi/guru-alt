# Guru

[![CI](https://github.com/alykapasi/guru-alt/actions/workflows/ci.yml/badge.svg)](https://github.com/alykapasi/guru-alt/actions/workflows/ci.yml)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
![React 19](https://img.shields.io/badge/react-19-61dafb)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

**An AI-first personalized learning platform built for *durable* learning — knowledge that sticks.**

Guru builds a knowledge graph for whatever you want to learn, measures what you actually know
concept by concept, schedules reviews before you forget, and tutors you from your own material.
Content, pacing, scaffolding and assessment adapt to each learner — and every adaptation is backed by
evidence the learner can inspect.

*"alt" is a version suffix for the current build, not part of the product name.*

---

## How it works

- **Knowledge graph.** Every subject is a hierarchy of Subject → Topic → Knowledge Component (KC),
  with prerequisites between KCs.
- **Continuous mastery model.** A dynamic Elo/IRT tracer keeps an ability *and* an uncertainty per
  KC, rolled up into topic and subject scores. Partial credit counts; a KC is mastered only when the
  conservative estimate `ability − 2·uncertainty ≥ 0.5` holds on measured evidence.
- **Retention.** FSRS schedules reviews independently of ability. A flashcard self-rating moves the
  schedule, never the mastery estimate.
- **LLM rubric grading.** Free-text answers are graded against a rubric, so partial understanding
  feeds the tracer instead of being rounded to right/wrong.
- **Adaptive lesson plans.** A policy picks the next KC from mastery, prerequisites and goals, and
  proposes prerequisite detours the learner can take, skip, or disprove.
- **Learner profile.** Behaviour-derived dimensions of *how* someone learns (not VARK), plus explicit
  settings that always win over inference.
- **Grounded tutoring.** Chat, agentic and guided-practice modes answer from the learner's own
  uploads — documents, scans, audio and video — with citations and an honest coverage label.

The full rationale is in [docs/MASTERPLAN.md](docs/MASTERPLAN.md) §4.

---

## Status

Invited-alpha build, heading toward independent use by invited adults after founder testing.
Workstreams 1–5 of the v0 delivery sequence in [docs/V0_DECISIONS.md](docs/V0_DECISIONS.md) are
built. Workstream 6 (evaluation and release gates) is next, then 7 (operating the invited alpha).
Passing tests show the software works; they do not show that anyone learns from it, and no
threshold has been calibrated on real learners yet.

| Area | State |
| ---- | ----- |
| Knowledge graph · tracer · FSRS · rubric grading · lesson-plan policy | ✅ Built |
| Multimodal ingestion (docs · vision OCR · Whisper ASR) + hybrid RAG | ✅ Built — URL import and web access are disabled for v0 |
| Tutoring: chat · agentic tools · guided-practice workflow (LangGraph) | ✅ Built |
| React frontend: chat, lessons, dashboard, uploads, notes, memory, account | ✅ Built |
| Notes with format projections and revision history | ✅ Built |
| Hosted identity (Clerk), invitations, suspension, audited admin visits | ✅ Built — verified against a Clerk dev instance, not yet a production deployment |
| Private ownership + reviewed publication of subjects | ✅ Built |
| Goals, guidance and detours; self-rating ≠ evidence; cross-subject links | ✅ Built — thresholds not yet calibrated |
| Grading provenance and re-grading; delayed retention and transfer checks | ✅ Built |
| Source scope + sources-only mode; versioned re-ingestion that keeps citations | ✅ Built |
| Archive / delete / forget; account deletion with a 7-day recovery window | ✅ Built |
| Metered LLM calls with per-learner and deployment spend caps | ✅ Built |
| Request deadlines, Stop, and provider busy/down handling | ✅ Built — deadlines not yet calibrated |
| Restart-safe paused practice; resumable, bounded ingestion | ✅ Built |
| Phone-width layout and accessibility, checked by axe in browser tests | ✅ Built — manual screen-reader pass pending |
| Query budgets for long histories; alert polling and history | ✅ Built |
| Explicit learner preferences (global + per subject) | ✅ Built |
| Evaluation: sweeps + MLflow, real-data datasets, DSPy compilation | 🚧 Partial — workstream 6 |
| Production deployment, mail, alert delivery, calibration | ☐ Open — workstreams 6–7 |

Per-phase detail lives in [docs/ROADMAP.md](docs/ROADMAP.md).

---

## Architecture

A layered FastAPI backend with a React + Vite frontend. The rule that holds it together:
**application code asks for an LLM by *role*, never by model name, and never imports a provider
SDK.**

```text
React app ─► API routers ─► services ─► { learning engine · agent graphs · rag · memory } ─► llm roles ─► provider
                                   │
                                   └─► taskiq worker (Redis): ingestion, memory write-back, profile
                                       refresh, erasure, alert polling
```

| Seam | What it hides |
| ---- | ------------- |
| `app/llm/` — model-role registry | `FAST` / `SMART` / `GENIUS` / `VISION` / `EMBED` → `(provider, model)` per environment. Ollama in dev, OpenRouter or Anthropic in prod, a `FakeProvider` in tests. Every call is recorded and admitted against spend caps. |
| `app/learning/` — `KnowledgeTracer` | The mastery estimator. Continuous Elo/IRT today; the KC-tagged event log is what a later DKT model trains on. |
| `app/core/identity.py` — identity provider | The only module that imports Clerk. A Clerk token is exchanged once for Guru's own session cookie, so nothing downstream knows who proved the identity. |
| `app/services/knowledge.py` — visibility | `is_visible_to` / `is_writable_by` are the whole authorization model for the graph. |
| `app/rag/scope.py` + `app/services/grounding.py` | What any generation may read, and what the tutor is told about it. |
| `app/services/removal.py` | The one place that decides what derives from what, for archive, delete and forget. |
| `app/llm/decisions.py` — Jev | Typed first-pass judgements (TypeSafe) in front of the FAST gate and SMART grader; each is off, shadow or live on its own. |
| `app/prompts/` — `RoleLM` | The only bridge from DSPy to the role registry. |
| `app/storage/` · `app/workers/` | S3-compatible blob store (RustFS in dev) · taskiq broker (in-memory in tests). |

Engineering detail: [docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md).

---

## Tech stack

- **Backend:** Python 3.13 · FastAPI · Pydantic v2 · async SQLAlchemy 2.0 · Alembic · taskiq + Redis
- **Data:** PostgreSQL 17 + pgvector (HNSW) + pg_trgm/GIN + full-text search · S3-compatible storage
- **AI:** role-addressed LLM layer · LangGraph · DSPy · FSRS · faster-whisper · MLflow
- **Frontend:** React 19 · TypeScript · Vite · React Router · TanStack Query · Clerk
- **Tooling:** uv · ruff · ty · beartype · pytest · poethepoet · pre-commit · Playwright

---

## Quickstart

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/), Docker, Node 20.19+, and
[Ollama](https://ollama.com/) (the dev default for every model role). Optional: `ffmpeg` for video
ingestion and `uv sync --extra asr` for audio transcription.

```bash
uv sync                                   # backend dependencies
cp .env.example .env                      # local settings; every field has a dev default
docker compose up -d                      # Postgres (pgvector) · Redis · RustFS
uv run poe db-upgrade                     # apply migrations
ollama pull llama3.2 && ollama pull llama3.2-vision && ollama pull nomic-embed-text
uv run poe hooks-install                  # optional: git pre-commit hooks
```

Then run three processes:

```bash
uv run poe dev                            # API → http://localhost:8000  (docs at /docs)
uv run poe worker                         # background jobs: ingestion, memory, erasure
cd frontend && npm install && npm run dev # app → http://localhost:5173
```

> **After every pull, run `uv run poe db-upgrade`.** A database behind the code fails at query
> time. Sign-in and the worker are usually the first places it shows.
>
> Postgres is published on host port **5433** to avoid clashing with a local install.

### Signing in locally

The simplest way in is the **Development sign-in** button on the sign-in page of a dev build. It
logs you in as a fixed dev learner while `GURU_DEV_AUTO_LOGIN` is on, which is the default.

To use real sign-in, create a Clerk application and set `GURU_CLERK_SECRET_KEY` and
`GURU_CLERK_JWT_KEY` in `.env`, and `VITE_CLERK_PUBLISHABLE_KEY` in `frontend/.env.local`. Guru is
invite-only, so the first account has to be let in from the command line:

```bash
uv run poe invite you@example.com --no-send   # record an invitation for your address
# sign in through the app, then:
uv run poe grant-admin you@example.com        # make that account an administrator
```

Everyone after that is invited from the admin portal. See [docs/RUNBOOK.md](docs/RUNBOOK.md) §11.

---

## Commands

Backend tasks run through poethepoet; `uv run poe --help` lists them all.

| Command | What it does |
| ------- | ------------ |
| `uv run poe check` | **The gate:** lint + type-check + tests. Every change leaves this green. |
| `uv run poe test` / `test-watch` | Tests, on their own `_test` database |
| `uv run poe db-upgrade` / `db-downgrade` | Apply / roll back one migration |
| `uv run poe db-check` | Fail if a model has drifted from the migrations (CI runs this) |
| `uv run poe lint` · `format` · `type-check` | ruff · ruff format · ty |
| `uv run poe invite` · `grant-admin` | Bootstrap the first account |
| `uv run poe reindex` | Re-embed sources in place; resumable |
| `uv run poe backup-drill` | Dump, restore to a scratch DB, compare row counts |
| `uv run poe eval` | Deterministic offline eval (grading + tracer suites) |
| `uv run poe sweep <config.yaml>` ⚠️ | Prompt × model × config sweep, logged to MLflow |
| `uv run poe compile-prompt <module>` ⚠️ | DSPy compile of a prompt module |
| `uv run poe decision-report` | Read Jev's shadow decisions before switching one live |
| `uv run poe perf-report` | Time the hot paths against a seeded long-history learner |
| `uv run poe regrade` ⚠️ | Re-grade past answers and report agreement; never changes a grade |

⚠️ calls the configured models, which costs money on a paid provider; none of these are part of
`poe check`.

Frontend, from `frontend/`: `npm run dev` · `npm run build` (the type gate) · `npm run lint` ·
`npm test` · `npm run e2e` (Playwright) · `npm run gen:api` (regenerate API types from the running backend).

---

## Configuration

Settings are `GURU_`-prefixed environment variables loaded into
[`app/core/config.py`](app/core/config.py); [`.env.example`](.env.example) documents every one.
The ones you are most likely to touch:

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `GURU_DATABASE_URL` | `postgresql+asyncpg://guru:guru@localhost:5433/guru` | Postgres |
| `GURU_REDIS_URL` | `redis://localhost:6379/0` | Job queue |
| `GURU_MODEL_FAST` · `_SMART` · `_GENIUS` | `ollama:llama3.2` | `provider:model` per role — `ollama`, `openrouter`, `anthropic` |
| `GURU_MODEL_VISION` | `ollama:llama3.2-vision` | Must be multimodal (OCR) |
| `GURU_MODEL_EMBED` | `ollama:nomic-embed-text` | Changing its dimension is a schema migration |
| `GURU_OPENROUTER_API_KEY` · `GURU_ANTHROPIC_API_KEY` | — | Only for cloud providers |
| `GURU_CLERK_SECRET_KEY` · `GURU_CLERK_JWT_KEY` | — | Hosted sign-in; unset means dev sign-in only |
| `GURU_DEV_AUTO_LOGIN` | `true` | Must be `false` in production, which refuses to boot otherwise |
| `GURU_IMPERSONATION_ENABLED` | `false` | Audited, reason-required administrator visits |

---

## Testing

The suite runs **offline and deterministically**: LLM calls go through a `FakeProvider`; ASR, video
demux and the job broker are faked; and tests use their own database, created and migrated by
`poe test`. A few tests that exercise a real local model are opt-in with `GURU_LIVE_MODEL_TESTS=1`.
The frontend has Vitest component tests and Playwright browser journeys.

`tests/eval/` is the measurement layer: a golden/live harness, a sweep runner with MLflow tracking,
datasets mined from the real event log, and DSPy compilation with measured deltas.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every push to `main` and every
PR: format, lint, type-check and tests; migrations applied, checked for drift and round-tripped;
a real Redis queue test; the API contract against the frontend's generated types; frontend lint,
tests and build; and the Playwright browser journeys, with axe failing on serious violations.

---

## Project layout

```text
app/
  api/v1/      versioned routers
  core/        config, db, identity, logging, middleware
  models/      SQLAlchemy models        schemas/   Pydantic boundary types
  services/    business logic behind the routers
  learning/    graph · tracer · FSRS · profile · lesson policy · notes
  agent/       LangGraph graphs          prompts/   DSPy programs + RoleLM
  rag/         ingestion + retrieval     memory/    per-learner memory
  llm/         role registry, providers, metering, Jev decisions
  storage/     blob store                workers/   taskiq broker, jobs, CLI tasks
frontend/      React 19 + TypeScript + Vite
db/migrations/ Alembic
tests/         pytest suite; tests/eval/ for measurement
docs/          design, roadmap, runbook, operations
```

---

## Documentation

| Document | Read it for |
| -------- | ----------- |
| [MASTERPLAN](docs/MASTERPLAN.md) | Vision, the learning engine, domain model, key decisions and why |
| [ROADMAP](docs/ROADMAP.md) | Phased plan and what landed in each phase |
| [V0_DECISIONS](docs/V0_DECISIONS.md) | Accepted invited-alpha decisions and the remaining delivery work |
| [TECHNICAL_DESIGN](docs/TECHNICAL_DESIGN.md) | LLM stack, orchestration, ingestion, tracer maths, testing |
| [RUNBOOK](docs/RUNBOOK.md) | Developing against it: ingestion, sign-in, sweeps, common failures |
| [OPERATIONS](docs/OPERATIONS.md) | Deploying, monitoring and recovering it |

---

## Principles

Pragmatic Programmer (DRY, YAGNI), small verified increments, async throughout, and type safety at
every layer — Pydantic at the boundaries, beartype at runtime, ty statically. Heavy dependencies
arrive at the phase that needs them, behind thin seams.

---

## License

Guru is licensed under the [GNU Affero General Public License v3.0](LICENSE). You may use, modify
and redistribute it; if you run a modified version as a network service, you must offer its users
the corresponding source code.
