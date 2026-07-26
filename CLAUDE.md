# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Product Direction — read these first

The source of truth for *what* we are building and *in what order* lives in two documents. Read them
before any non-trivial work:

- **[docs/MASTERPLAN.md](docs/MASTERPLAN.md)** — stable north star: vision, the learning engine,
  domain model, target architecture, key decisions + rationale, and what's deferred.
- **[docs/ROADMAP.md](docs/ROADMAP.md)** — phased, shippable execution plan (Phase 0 → 8).
- **[docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md)** — engineering detail: LLM stack &
  model-role registry, LangGraph orchestration, prompt optimization, multimodal ingestion, the
  learning-engine data model + tracer math, testing, and dependency timing.

## Project Overview

**Guru** is an **AI-first personalized learning platform** whose promise is *durable* learning —
knowledge that sticks. It adapts content, pacing, scaffolding, and assessment to each learner. The
larger mission is a flipped-classroom model with schools/govts to raise the educational floor. The
MVP targets adult self-directed learners; tiers roll out top-down (adult → college → … → pre-K).
*("alt" is a version suffix, not part of the name.)*

**Core engine (the part to get right):** a hierarchical **knowledge graph** (Subject → Topic → KC),
a **continuous dynamic-IRT/Elo** learner model with per-KC ability + uncertainty rolled up into
subject scores, **FSRS** for retention, **LLM rubric grading** for partial credit, and an adaptive
**lesson-plan policy**. A swappable `KnowledgeTracer` interface + KC-tagged event log earn a **DKT**
upgrade later. See MASTERPLAN §4 for the full rationale (incl. why continuous IRT over binary BKT).

**Tech Stack:**

- **Backend:** Python 3.13+ with FastAPI, Uvicorn, Pydantic, asyncio; beartype (runtime types), ty
  (static types), ruff, pytest, poethepoet — all via `uv`.
- **Frontend:** React + TypeScript + Vite (later phase).
- **Database:** PostgreSQL with pgvector (HNSW), pg_trgm/GIN, tsvector full-text.
- **LLM:** provider-agnostic abstraction, Claude by default with model routing.
- **Principles:** Pragmatic Programmer, DRY, YAGNI, conciseness + performance.

## Architecture Overview

Layered FastAPI backend (see MASTERPLAN §6 and TECHNICAL_DESIGN for the full picture). Key
non-obvious modules:

- `app/llm/` — provider-agnostic LLM + **model-role registry** (`FAST`/`SMART`/`GENIUS`/`EMBED` →
  `(provider, model)` per env). Providers: Ollama (dev), OpenRouter (prod), Anthropic, `FakeProvider`
  (tests). **Code references roles, never model names; services never call a provider SDK directly.**
- `app/prompts/` — DSPy modules + the **interactive prompt-refinement gate** (HITL loop that
  co-constructs the learner's goal until they're satisfied).
- `app/agent/` — **LangGraph** stateful graphs (tutoring turn, lesson-gen, grading, content assembly)
  with a tool registry; the three modes (chat / agentic / workflow) are composable graphs.
- `app/rag/` — **multimodal ingestion** (docs / vision-LLM OCR / Whisper ASR / weblinks →
  normalize → chunk → embed → store w/ provenance) + hybrid retrieval (vector + full-text + metadata).
- `app/learning/` — the education core: knowledge graph, `KnowledgeTracer` (continuous Elo/Glicko +
  uncertainty, hierarchical roll-up), FSRS, **learner profile** (behavior-derived "how they learn"
  dimensions), lesson-plan policy, assessment + grading, content assembly, analytics. The full
  *learner model* = mastery model + learner profile.
- `app/memory/` — persistent per-learner memory.

Cross-cutting: SSE for token streaming · `learner_id` threaded everywhere behind a **stubbed auth
seam** · **token/cost logged per LLM call** (tagged by role+model) from day one · Redis-backed job
queue (taskiq/arq) introduced at the ingestion phase · heavy deps (LangGraph/DSPy) enter at the phase
that needs them, behind thin seams.

## Development Workflow

**Core principle:** Plan small changes, implement incrementally, verify with user before next step.

1. **Before coding:** Discuss approach and verify alignment
2. **Implementation:** Small, focused commits
3. **Testing:** Verify changes work (run code, not just tests)
4. **Check-in:** Review with user before next iteration

## Setup & Commands

All commands are orchestrated via `poethepoet`. Run `uv run poe --help` to list all tasks.

**Backend:**

- `uv run poe dev` — Start FastAPI dev server with auto-reload
- `uv run poe test` — Run all tests
- `uv run poe test-watch` — Run tests in watch mode (fail-fast)
- `uv run poe type-check` — Run ty type checker
- `uv run poe lint` — Check code with ruff
- `uv run poe format` — Auto-format with ruff
- `uv run poe format-check` — Check formatting without modifying
- `uv run poe check` — Aggregate gate: lint + type-check + test (every phase ends green on this)
- `uv run poe db-upgrade` — Run database migrations
- `uv run poe db-downgrade` — Rollback one migration

> Note: `poe check` is introduced in Phase 0. Until then, run lint/type-check/test individually.

**Frontend (when ready):**

- `npm run dev` — Vite dev server
- `npm run build` — Production build
- `npm run lint` — ESLint + Prettier

## Key Technical Decisions

See MASTERPLAN §7 for the full decision table + rationale. The load-bearing ones:

- **Continuous IRT/Elo over binary BKT** — captures partial understanding; binary loses signal.
- **Per-KC mastery, hierarchically rolled up** — a single subject score is lossy.
- **Learner profile = evidence-based behavioral dimensions, not VARK** — modality is a UX signal
  only; matching instruction to a "learning style" has no replicated effect on outcomes.
- **LLM rubric grading from v1** — enables partial credit feeding the tracer.
- **Swappable `KnowledgeTracer` + event log day one** — earns DKT training data for free.
- **LLM by role, not model** — `FAST`/`SMART`/`GENIUS`/`EMBED` map to `(provider, model)` per env;
  Ollama for dev, OpenRouter for prod, Claude as the prod SMART/GENIUS default.
- **LangGraph orchestration + interactive refinement gate + DSPy** — introduced when they earn it.
- **Multimodal ingestion** (docs/OCR/ASR/web) into one provenance-tagged RAG pipeline.
- **Auth stubbed behind a seam** — thread `learner_id` everywhere now; real auth in Phase 10.
- **Pydantic at boundaries · async throughout · Alembic-tracked schema.**

## Performance & Conciseness Guidelines

- Favor readability over clever optimizations; profile before optimizing
- Use Pydantic validators to prevent invalid data at boundaries
- Index strategically (GIN for searches, pgvector for similarity)
- Async operations for I/O; synchronous for CPU-bound logic
- Keep endpoint logic focused; move complex business logic to services

## Testing Strategy

- Unit tests for validators, utilities, business logic
- Integration tests for API endpoints
- Database tests use transactions (rollback after each test)
- Avoid mocking the database; use test fixtures instead

## When to Check In

- Before major architectural decisions
- After completing a feature slice (even if small)
- When uncertain about approach
- Before refactoring or cleanup

## When working on LLM / Anthropic code

All LLM access goes through `app/llm/` by **role** (`FAST`/`SMART`/`GENIUS`/`EMBED`) — never call a
provider SDK, and never hardcode a model name, from a service, router, or LangGraph node. The
role→model map is config, per environment (see TECHNICAL_DESIGN §3). Use the `claude-api` skill for
current Claude ids, params, streaming, and tool use. Prod example map: `SMART`=`claude-sonnet-4-6`,
`GENIUS`=`claude-opus-4-8`, `FAST`=`claude-haiku-4-5` or a cheap OSS model; dev runs `FAST` on Ollama
and routes `SMART`/`GENIUS` to OpenRouter.

---

*Product direction is governed by [docs/MASTERPLAN.md](docs/MASTERPLAN.md) and
[docs/ROADMAP.md](docs/ROADMAP.md). Keep them in sync as decisions evolve.*
