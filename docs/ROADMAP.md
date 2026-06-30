# Guru — Phased Roadmap

> **Status:** Living execution plan. The *what & why* lives in [MASTERPLAN.md](./MASTERPLAN.md),
> deeper engineering detail in [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md); this file is *how & in
> what order*. Built for the working rhythm: **plan → small change → check in → repeat.** Each phase
> is independently shippable and verifiable. Check off items as they land.

**Working agreement**

- Keep changes small and reviewable; check in with the maintainer between slices.
- Every phase ends green: `uv run poe check` (lint + type + test) passes.
- Prefer reusing existing utilities/patterns over new code.
- Don't build deferred items (see Masterplan §9) before their phase.

**Legend:** ☐ todo ☑ done · **DoD** = Definition of Done.

---

## Phase 0 — Foundations

**Goal:** a clean, runnable skeleton with the full quality gate wired up.

**Scope**
- ☑ `app/core/config.py` — typed settings via `pydantic-settings`; `.env` + committed `.env.example`.
- ☑ `app/core/db.py` — async SQLAlchemy 2.0 engine + session (asyncpg); FastAPI dependency + `Base`.
- ☑ `app/core/logging.py` — structured logging (structlog) with request IDs (`app/core/middleware.py`).
- ☑ beartype claw enabled for runtime type checking in dev/test (`app/__init__.py`, prod-guarded).
- ☑ Alembic initialized under `db/migrations/`; first migration enables `vector` + `pg_trgm`.
- ☑ `docker-compose.yml` — Postgres (pgvector, host **5433**) + Redis for local dev.
- ☑ `poe check` aggregate task = `lint` + `type-check` + `test`.
- ☑ **GitHub Actions CI** (`.github/workflows/ci.yml`) — format-check, lint, type-check, test, and a
  migration-apply check against a pgvector service. *CD deferred to Phase 8 (no deploy target yet).*
- ☐ Pre-commit hooks (ruff format/lint, etc.) — follow-up.

**DoD:** ✅ `uv run poe check` passes · ✅ `uv run poe dev` boots, `GET /health` returns ok ·
✅ `docker compose up` brings up Postgres+pgvector · ✅ first migration applies extensions cleanly ·
✅ CI workflow valid (lockfile frozen-ready).

> Done. Note: host Postgres port is **5433** (5432 was occupied); `greenlet` added for SQLAlchemy async.
> CI runs once the repo is pushed to GitHub (not yet a git repo).

---

## Phase 1 — Domain core & knowledge graph

**Goal:** the backbone data model exists and is queryable.

**Scope**
- ☑ ORM models: `Learner` (stub identity), `Subject`, `Topic`, `KC`, prerequisite edges (`KCEdge`),
  `LearnerKCState`, `LearningEvent` (`app/models/`, UUID PKs + timestamp mixins).
- ☑ Pydantic schemas + CRUD for the knowledge graph (`app/schemas/`, `app/services/`, `app/api/v1/`).
- ☑ Stub auth dependency (`get_current_learner`) creating a dev learner on first use; `learner_id`
  threaded through every endpoint from the start (`app/api/deps.py`).
- ☑ Alembic migration `0002_knowledge_core`. Service + API integration tests with transactional
  rollback isolation (`tests/conftest.py`).

**DoD:** ✅ build a Subject→Topic→KC graph with prerequisites via the API and read it back (covered by
`tests/test_knowledge.py`) · ✅ tests cover model + CRUD (13 tests) · ✅ migration applies (and
round-trips down/up).

> Done. Endpoints under `/api/v1` (subjects/topics/kcs/prerequisites); 404/409/400/422 handled.

---

## Phase 2 — LLM abstraction + model-role registry + tutor chat

**Goal:** talk to a guru through a provider-agnostic, role-based, testable LLM layer.

**Scope**
- ☐ `app/llm/` provider-agnostic interface; unified message + tool schema; embeddings method.
- ☐ **Model-role registry**: `FAST` / `SMART` / `GENIUS` / `EMBED` → `(provider, model)` resolved
  from config **per environment**. Code references roles only.
- ☐ Providers: **Ollama** (local dev), **OpenRouter** (cloud/prod), `AnthropicProvider`, and
  `FakeProvider` (deterministic tests). Dev maps `FAST`→Ollama, `SMART`/`GENIUS`→cheap OpenRouter.
- ☐ SSE streaming chat endpoint with a tutor persona; persisted `Conversation` / `Message`.
- ☐ Per-call **token & cost logging** (tagged by role + model).

**DoD:** streaming chat works end-to-end via the registry against a real model; switching a role's
model is a config change with no code edits; unit tests run offline on `FakeProvider`; an Ollama
integration test exercises real local generation; token/cost recorded per call.

> Use the `claude-api` skill when implementing the Anthropic provider. Prod role map (example):
> `SMART`=`claude-sonnet-4-6`, `GENIUS`=`claude-opus-4-8`, `FAST`=cheap OSS/Haiku. Concrete model
> names are config values (see TECHNICAL_DESIGN).

---

## Phase 3 — Knowledge tracer + assessment loop

**Goal:** the engine that makes Guru adaptive — minimal but end-to-end.

**Scope**
- ☐ `KnowledgeTracer` interface: `estimate / update / due_reviews`.
- ☐ Continuous **Elo/Glicko** estimator per KC with **uncertainty**; online updates.
- ☐ **Hierarchical roll-up** KC → Topic → Subject (weighted, uncertainty propagated); multi-KC
  credit apportioning.
- ☐ Assessment item models (MCQ, cloze, short, long) + per-KC rubrics.
- ☐ Auto-grading + **LLM rubric grading → graded/partial-credit** observations.
- ☐ **FSRS** scheduler for reviews.
- ☐ KC-tagged `LearningEvent` logging on every interaction (also the substrate the **learner
  profile** estimators consume in Phase 5 — capture latency, hints, correctness richly now).
- ☐ Seed of the **eval harness** (grading reliability + tracer sanity).

**DoD:** answering items updates per-KC ability + uncertainty, rolls up to subject, schedules
reviews via FSRS, and writes a replayable event; rubric grading yields partial credit; tests +
first eval cases pass.

---

## Phase 4 — Multimodal ingestion + content engine + RAG

**Goal:** real teaching material — ingested from any source, reusable, grounded, cost-controlled.

**Scope**
- ☐ **Ingestion source adapters** normalize into one `document → chunks → embeddings + provenance`
  pipeline, run as Redis-backed background jobs — shipped in two waves (4a then 4b).
- ☐ **4a (text-first):** office docs — PDF, PPTX, DOCX, XLSX, TXT — + **public weblinks**
  (fetch + readability extraction).
- ☐ **4b (heavier modalities):** **handwritten notes via vision-LLM OCR**; **audio/video via ASR**
  (Whisper; video → demux audio, optional keyframes for slides).
- ☐ Content building blocks (lessons, brief + comprehensive wikis, question banks): AI-generated,
  **KC-tagged**, cached/reusable, **assembled + personalized** per learner.
- ☐ **Hybrid retrieval** (pgvector HNSW + tsvector/GIN + metadata filter); grounded generation with
  provenance/citations.
- ☐ Embeddings via the `EMBED` role; ingestion + content pre-generation on the queue.

**DoD:** a learner can upload a PDF, a handwritten photo, an audio file, and paste a URL — all become
retrievable, provenance-tagged chunks; a learner goal yields assembled, KC-tagged lessons/wikis
grounded in retrieved knowledge with citations; generation reuses cached blocks where possible.

> Sequence 4a before 4b: ship text/doc/web ingestion first (covers most value), then add OCR/ASR.

---

## Phase 5 — Orchestration + lesson-plan policy + placement + profile + study aids + memory

**Goal:** the guided, personalized journey comes together on a real orchestration substrate.

**Scope**
- ☐ Introduce **LangGraph** as the orchestration substrate; migrate the tutoring turn + lesson
  generation into stateful graphs (nodes pull models by role from the registry).
- ☐ **Interactive prompt-refinement gate** (HITL subgraph): co-constructs the learner's goal/prompt
  in a loop — propose → learner feedback → refine — **until the learner is satisfied**, then commits
  to generation. Doubles as a placement/metacognition moment + profile cold-start signal.
- ☐ Placement diagnostic (light test + inference + asking) → seed KC priors.
- ☐ **Learner profile**: `LearnerProfile` interface + first behavior-first estimators (pace,
  optimal-challenge, error-type, help-seeking, calibration, interests) reading the event log; trait/
  state + uncertainty; cold-start from intake + refinement gate + placement; learner view/reset.
- ☐ Adaptive **lesson-plan generator/policy** reading mastery **and** profile: objectives →
  prerequisite-ordered KCs, scaffolding by tier, **profile-driven** step size / challenge / hint
  policy / example selection; revised on evidence.
- ☐ Session runner that follows/updates the plan.
- ☐ Study aids: flashcards, spaced-repetition surfacing (FSRS due reviews), fill-in-the-blank.
- ☐ Per-user **memory** subsystem wired into sessions (facts, preferences, summarization, write-back).

**DoD:** a new learner co-constructs a goal through the interactive gate, is placed, gets an adaptive
plan whose pacing/challenge demonstrably shift with profile values (e.g. faster pace → larger steps),
runs LangGraph-orchestrated sessions with study aids and surfaced reviews, and the experience reflects
persistent memory across sessions.

---

## Phase 6 — Agentic tools & workflows

**Goal:** the unified engine's agentic and workflow modes.

**Scope**
- ☐ Tool registry; live/external-data tool; retrieval-as-tool — exposed as LangGraph tool nodes.
- ☐ Unified engine exposing chat / agentic / workflow modes as composable LangGraph graphs.
- ☐ First structured workflow graph (e.g., guided practice / worked-example walkthrough).

**DoD:** a guru can take a tool-using action and run at least one structured workflow end-to-end,
selected by need/tier.

---

## Phase 7 — Frontend MVP (React + Vite)

**Goal:** the learner-facing product.

**Scope**
- ☐ Vite + React + TS app; typed API client generated from the OpenAPI schema.
- ☐ Streaming tutor chat UI; lesson-plan + session UI.
- ☐ **Statistics dashboard:** mastery at KC/topic/subject levels, retention curves, momentum, and the
  **learner profile** ("how you learn" — with view/reset); tasteful **mastery-based** gamification
  (rewards tied to learning, not time-on-app).
- ☐ Uploads; conversation/session history.

**DoD:** an adult learner completes the full loop in the browser — place → plan → session → assess →
see the dashboard update — with streaming responses.

---

## Phase 8 — Hardening & scale

**Goal:** production-readiness.

**Scope**
- ☐ Real authentication; rate limiting; caching.
- ☐ Robust queue/workers; tracing + cost dashboards.
- ☐ Deployment pipeline; prod provider hardening (OpenRouter → hyperscaler option for compliance).
- ☐ Expanded eval harness (educational quality + grading reliability) as a release gate.
- ☐ **DSPy optimization workstream**: build eval datasets from the KC-tagged event log; compile
  internal modules (grading, refinement gate, content generation) against metrics; gate prompt
  changes on eval deltas.

**DoD:** deployable with real auth, observability, and an eval gate that blocks regressions in
teaching/grading quality; DSPy-compiled modules measurably beat hand-written prompts on the eval set.

---

## Deferred Backlog (see Masterplan §9)

DKT · standards alignment · teacher/school B2B + dashboards · multi-tenant orgs/classes ·
younger tiers + COPPA/FERPA/GDPR-K · offline / low-bandwidth · mobile & desktop · billing.
