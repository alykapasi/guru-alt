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
- ☑ `app/llm/` provider-agnostic interface; unified message + tool schema; embeddings method.
- ☑ **Model-role registry**: `FAST` / `SMART` / `GENIUS` / `EMBED` → `(provider, model)` resolved
  from config **per environment**. Code references roles only.
- ☑ Providers: **Ollama** (local dev), **OpenRouter** (cloud/prod), `AnthropicProvider`, and
  `FakeProvider` (deterministic tests). Dev maps `FAST`→Ollama, `SMART`/`GENIUS`→cheap OpenRouter.
- ☑ SSE streaming chat endpoint with a tutor persona; persisted `Conversation` / `Message`.
- ☑ Per-call **token & cost logging** (tagged by role + model).

**DoD:** streaming chat works end-to-end via the registry against a real model; switching a role's
model is a config change with no code edits; unit tests run offline on `FakeProvider`; an Ollama
integration test exercises real local generation; token/cost recorded per call.

> Use the `claude-api` skill when implementing the Anthropic provider. Prod role map (example):
> `SMART`=`claude-sonnet-4-6`, `GENIUS`=`claude-opus-4-8`, `FAST`=cheap OSS/Haiku. Concrete model
> names are config values (see TECHNICAL_DESIGN).

> Done. `OpenAICompatProvider` serves Ollama + OpenRouter; `AnthropicProvider` uses the native SDK
> (model-agnostic, no sampling params). SSE tutor chat at `POST /api/v1/conversations/{id}/messages`
> streams tokens and persists `Conversation`/`Message`/`LLMCall` (token+cost per call). Offline tests
> on `FakeProvider`; a skippable Ollama integration test exercises real local generation.

---

## Phase 3 — Knowledge tracer + assessment loop

**Goal:** the engine that makes Guru adaptive — minimal but end-to-end.

**Scope**
- ☑ `KnowledgeTracer` interface: `estimate / update / due_reviews`.
- ☑ Continuous **Elo/Glicko** estimator per KC with **uncertainty**; online updates.
- ☑ **Hierarchical roll-up** KC → Topic → Subject (weighted, uncertainty propagated); multi-KC
  credit apportioning.
- ☑ Assessment item models (MCQ, cloze, short, long) + per-KC rubrics.
- ☑ Auto-grading + **LLM rubric grading → graded/partial-credit** observations.
- ☑ **FSRS** scheduler for reviews.
- ☑ KC-tagged `LearningEvent` logging on every interaction (also the substrate the **learner
  profile** estimators consume in Phase 5 — capture latency, hints, correctness richly now).
- ☑ Seed of the **eval harness** (grading reliability + tracer sanity).

**DoD:** answering items updates per-KC ability + uncertainty, rolls up to subject, schedules
reviews via FSRS, and writes a replayable event; rubric grading yields partial credit; tests +
first eval cases pass.

**Done.** Tracer is a Glicko-style estimator on the logit scale (`E = sigmoid(θ − d)`, precision-
weighted Bayesian update, `√(RD² + c²·t)` decay) behind a swappable `MasteryEstimator`; the DB-backed
`KnowledgeTracer` (`GlickoTracer` + `DEFAULT_TRACER`) wraps it with persistence, multi-KC apportioning,
roll-up, and FSRS. Grading dispatches auto (MCQ/cloze/fill-blank) · LLM rubric (short/long, SMART
role) · self (flashcard → FSRS rating); the answer endpoint commits grade + per-KC state + events
atomically. FSRS state is stored opaquely as a serialized card; `GET /reviews/due` lists due KCs.
Eval harness seeds `tests/eval/` (golden grading + tracer suites gate CI; live rubric suite runs on
Ollama) with a `poe eval` runner.

---

## Phase 4 — Multimodal ingestion + content engine + RAG

**Goal:** real teaching material — ingested from any source, reusable, grounded, cost-controlled.

**Scope**
- ☑ **Ingestion source adapters** normalize into one `document → chunks → embeddings + provenance`
  pipeline, run as Redis-backed background jobs (**taskiq**) — shipped in two waves (4a then 4b).
- ☑ **4a (text-first):** office docs — PDF, PPTX, DOCX, XLSX, TXT — + **public weblinks**
  (fetch + readability extraction, robots-aware).
- ☑ **4b (heavier modalities):** **handwritten notes via vision-LLM OCR**; **audio/video via ASR**
  (Whisper; video → demux audio + keyframe OCR); **per-chunk KC auto-tagging**.
- ☑ Content building blocks (lessons, brief + comprehensive wikis, question banks): AI-generated,
  **KC-tagged**, cached/reusable, **assembled** per learner. *(Profile-driven personalization → Phase 5.)*
- ☑ **Hybrid retrieval** (pgvector HNSW + tsvector/GIN + metadata filter); grounded generation with
  provenance/citations.
- ☑ Embeddings via the `EMBED` role; ingestion + content pre-generation on the queue.

**DoD:** a learner can upload a PDF, a handwritten photo, an audio file, and paste a URL — all become
retrievable, provenance-tagged chunks; a learner goal yields assembled, KC-tagged lessons/wikis
grounded in retrieved knowledge with citations; generation reuses cached blocks where possible.

> Sequence 4a before 4b: ship text/doc/web ingestion first (covers most value), then add OCR/ASR.
>
> **4a done.** Object storage behind a `BlobStore` seam (S3/MinIO + in-memory); taskiq broker seam
> (Redis prod, in-memory tests); per-format adapters (PyMuPDF/python-docx/python-pptx/openpyxl/
> trafilatura) with page/slide/paragraph/sheet locators; idempotent extract→normalize→chunk→embed→store
> pipeline; `Source`/`Chunk` (`Vector(768)` HNSW + generated `tsvector` GIN) and `ContentBlock`
> (migrations 0007–0008); RRF hybrid retrieval with learner/subject/topic/source scope; cached,
> cited content engine. Eval harness extended with retrieval-recall + grounding-citation suites.
>
> **4b done.** Vision-capable LLM layer (`VISION` role) + image/scanned-PDF vision-OCR; large-document
> ingestion (streaming blob I/O at constant memory, bounded-concurrency OCR + batched embeds, EPUB);
> audio ASR behind a `Transcriber` seam (faster-whisper, optional `asr` extra); video behind a
> `MediaDemuxer` seam (ffmpeg demux → audio-track ASR + evenly-sampled keyframe OCR); per-chunk KC
> auto-tagging (`ChunkKC` + migration 0009 — FAST model tags each chunk against subject/topic-scoped
> candidate KCs). Eval harness extended with a live-model KC-tagging suite. **Phase 4 complete.**

---

## Phase 5 — Orchestration + lesson-plan policy + placement + profile + study aids + memory

**Goal:** the guided, personalized journey comes together on a real orchestration substrate.

**Scope**
- ☑ Introduce **LangGraph** as the orchestration substrate; migrate the tutoring turn + lesson
  generation into stateful graphs (nodes pull models by role from the registry).
- ☑ **Interactive prompt-refinement gate** (HITL subgraph): co-constructs the learner's goal/prompt
  in a loop — propose → learner feedback → refine — **until the learner is satisfied**, then commits
  to generation. Doubles as a placement/metacognition moment + profile cold-start signal.
- ☑ Placement diagnostic (light test + inference + asking) → seed KC priors.
- ☑ **Learner profile**: `LearnerProfile` interface + behavior-first estimators reading the event
  log; trait/state + uncertainty; cold-start from intake + refinement gate + placement; learner
  view/reset.
- ☑ Adaptive **lesson-plan generator/policy** reading mastery **and** profile: objectives →
  prerequisite-ordered KCs, scaffolding by tier, **profile-driven** step size / challenge / hint
  policy / example selection; revised on evidence.
- ☑ Session runner that follows/updates the plan.
- ☑ Study aids: flashcards, spaced-repetition surfacing (FSRS due reviews), fill-in-the-blank.
- ☑ Per-user **memory** subsystem wired into sessions (facts, preferences, summarization, write-back).

> **Substrate landed.** LangGraph introduced behind `app/agent/`; the tutor turn now runs as
> a single-node state graph (`build_tutor_graph`) driven by a `run_tutor_turn` service that
> owns persistence, with byte-identical SSE. Nodes call our role-based `LLMClient` (no
> LangChain models); token streaming rides the custom stream writer.
>
> **Refinement gate landed.** A second graph (`build_refinement_graph`, `app/agent/refinement.py`)
> implements `propose → ask_learner (HITL interrupt) → satisfied?/max-rounds → commit`, compiled
> with an `InMemorySaver` checkpointer so the pause/resume survives across HTTP requests within a
> process. No new endpoint: `POST /conversations/{id}/messages` (`app/api/v1/chat.py`) dispatches
> per call — goal-less + no history → start the gate; goal-less + mid-flight (checkpoint paused) →
> resume it; goal committed → plain tutor turn, now grounded by `Conversation.goal` in the system
> prompt. If checkpoint state is lost (process restart) mid-gate on a conversation with history,
> it degrades to plain chat rather than re-prompting for a goal. **Known limitation:** the
> checkpointer is process-local — not durable across restarts, not multi-worker-safe; fine for
> single-process Phase 5, revisit with a Postgres-backed saver before Phase 8 scaling.
>
> **Placement diagnostic landed.** No new graph — a plain two-endpoint flow (`app/api/v1/placement.py`):
> `GET /subjects/{id}/placement/prompt` returns a fixed background question, `POST
> /subjects/{id}/placement` (`app/services/placement.py`) infers rough per-KC starting levels
> from the learner's free-text answer (`app/learning/placement_inference.py`, FAST role,
> KC-tagging-style numbered-candidate JSON prompt), seeds `LearnerKCState` only for KCs with no
> existing state (`mastery.seed_prior` — never overwrites real evidence), and surfaces a light
> test of up to `placement_light_test_size` root KCs (no incoming prerequisite) for the learner
> to answer through the existing, unchanged `/items/{id}/answer` endpoint. Items are generated
> on demand (`app/learning/item_generation.py`, FAST-role MCQ generation — new capability, since
> the Phase 4 "question bank" content has no answer key/`Item` link) and persisted into the
> shared, learner-unscoped item bank, so later placements/lessons for that KC reuse them instead
> of regenerating. **Known limitation:** the `"some"`/`"strong"` → ability/uncertainty mapping is
> a reasonable-but-arbitrary v1 placeholder, not calibrated against real outcome data.
>
> **Learner profile landed.** EAV-shaped `learner_profiles`/`profile_dimensions`
> (`app/models/profile.py`) so the dimension catalog lives entirely in code
> (`DIMENSION_SPECS` in `app/learning/profile_estimators.py`) — adding or dropping a dimension
> is a code change, never a migration, which matters because the catalog is expected to be
> pruned once real usage data shows which dimensions are actually predictive. Twelve dimensions
> shipped across all four MASTERPLAN families, all backed by real estimators over data already
> captured (no new capture surfaces): *cognitive & pace* — `pace` (median latency + speed
> trend), `optimal_challenge` (productive-struggle difficulty band), `error_type` (one batched
> FAST-model call classifying wrong answers conceptual/procedural/careless), `cognitive_load_tolerance`
> (within-session accuracy drop); *metacognition* — `help_seeking` (mean hints/item),
> `persistence` (bounce-back rate after a wrong answer); *motivation & affect* — `engagement`
> (state; error-streak proxy over the most recent session only), `goal_orientation` (one FAST
> call classifying `Conversation.goal` as mastery- vs performance-oriented — the refinement
> gate's cold-start signal flows in with no extra wiring); *context & preferences* — `interests`
> (FAST-extracted topic tags from the learner's own messages), `reading_level` (pure
> Flesch-Kincaid-style grade level, no LLM call), `session_logistics` (typical session length +
> preferred hour from gap-clustered sessions), and `format_effectiveness` (score/difficulty by
> `Item.item_type` — the outcome-linked "which assessment format actually works for this
> learner" signal that future generation/format-selection decisions consume). Deliberately
> **not** shipped: `calibration` and `confidence` (MASTERPLAN-named, but no self-prediction or
> real affect signal exists yet to compute them honestly) and true delivery-modality VARK
> (visual/auditory/kinesthetic — no content-format tracking on interactions exists yet); all
> three are additions the catalog can absorb later with zero schema change. Refresh is
> **on-demand** (`POST /profile/refresh`), not triggered on every graded answer — recomputing a
> dozen dimensions (three of which call an LLM) after every item would be wasteful;
> `assessment.answer_item` stays untouched. Trait dimensions read a learner's full event
> history, state dimensions (`engagement`) read only the most recent session — no incremental
> EWMA blending in v1, since full recompute is cheap at this data scale. Learner view/reset:
> `GET /profile`, `POST /profile/{key}/reset`. Memory is the remaining Phase 5 slice.
>
> **Lesson-plan policy landed** — designed as a genuinely dynamic policy, not a document
> regenerated only on request. `LessonPlan` (`app/models/lesson_plan.py`, one row per
> `(learner_id, subject_id)`) stores `steps` carrying per-step **status**
> (`pending`/`active`/`done`), so the plan itself is a live record of what will be learned and
> what already has been — not just a fixed sequence. Generation is split into two tiers by cost:
> `generate_lesson_plan` (`app/services/lesson_plan.py`) is the expensive, on-demand path — one
> FAST-role objective-selection call over a numbered KC candidate list (same shape as
> placement/KC-tagging; a malformed/no-goal reply falls back to targeting the whole subject),
> prerequisite-closure (BFS) + topo-sort (Kahn's algorithm) over the KC DAG; `revise_plan` is the
> cheap, **auto-triggered** path — no LLM call, no topo-sort recompute, just a pure re-derivation
> (`revise_steps` in `app/learning/lesson_plan.py`) of status/order/hints over the *existing*
> step list. `revise_plan` runs automatically after every graded answer
> (`assessment.answer_item`, scoped to the touched KCs' subject) and after every profile refresh
> (`profile.refresh_profile`, across every plan the learner has) — both no-op if the learner has
> no plan for that subject yet. This is "revised on evidence" (TECHNICAL_DESIGN §7.7) taken
> literally: a mastered KC's step one-way-ratchets to `done`; a KC coming due for FSRS review
> gets inserted as a `review` step ahead of new material (soonest-due first) and flips to `done`
> once no longer due; `optimal_challenge`/`help_seeking`+`persistence`/`format_effectiveness`
> refresh every non-done step's `target_difficulty`/`hint_density`/`preferred_item_type`, while
> `pace`/`interests`/`reading_level` refresh the plan-level `pacing`/`example_tags`/
> `reading_level_hint` — every hint degrading to `None`/a neutral default if its source profile
> dimension hasn't been computed yet. The tutor conversation graph now **reads the plan**:
> `run_tutor_turn` folds the learner's active step (KC, target difficulty, hint density,
> preferred item type) into the system prompt the same way `Conversation.goal` already does — no
> `TutorState` schema change, no new graph node — so the plan actually drives generation instead
> of sitting beside it. Endpoints: `POST`/`GET /subjects/{id}/lesson-plan`. **Known v1
> simplifications:** "well mastered" (`ability >= 1.0, uncertainty <= 0.5`) is an arbitrary
> threshold, same spirit as placement's level→estimate mapping; "scaffolding by tier" collapses
> to one tier (adult-only MVP), so the profile is the whole adaptive lever for now; tutor-turn
> grounding uses the learner's **most-recently-updated plan** across all subjects, since
> conversations aren't subject-scoped yet — revisit when the session runner needs tighter
> per-conversation scoping. Full step-advancement through live conversation (marking a step done
> because a session covered it) is the session runner's job, next.
>
> **Session runner landed.** No new `Session` model and no new LangGraph graph/interrupt —
> "session" stays a derived concept (same spirit as the profile's `session_logistics`), and the
> runner is a small addition on top of the substrate that already existed: `Conversation` gains
> an optional `subject_id` (`app/models/chat.py`, set once at creation, never changed), which
> turns the previous slice's "most-recently-updated plan" heuristic in
> `lesson_plan.get_active_step_context` into an exact `(learner, subject)` lookup whenever a
> conversation is scoped to one — the heuristic itself is untouched and still the fallback for
> subject-less conversations. `app/services/session_runner.py::next_item` is the one new piece:
> it resolves the plan's active step and turns it into an actual practice item — reusing a bank
> item for the KC if one exists (`assessment.find_item_for_kc`), generating one (FAST role) only
> if the bank is empty, and working identically for `"new"` and `"review"` steps, so FSRS-due
> reviews surfaced by the plan get served as practice for free. `run_tutor_turn` calls it for
> subject-scoped conversations and attaches the result to the `done` SSE frame as a new optional
> `item` field — resolved fresh every turn, not tracked as "already served," since it's a
> separate structured field rather than something spliced into conversation history. Critically,
> **"updates the plan" adds no new mechanism**: the item is answered through the existing,
> unchanged `POST /items/{id}/answer` endpoint, which already runs the tracer and
> auto-`revise_plan` (previous slice) — the tracer stays the only thing that moves mastery, on
> purpose, rather than a second "the conversation seemed to cover this" signal running in
> parallel. Verified live end-to-end against real Postgres + Ollama: a served item answered
> through the real endpoint flipped its step to `done` and the very next turn's item correctly
> disappeared, all without a single lesson-plan endpoint call. **Known v1 simplifications:** the
> active step's `target_difficulty` hint is not yet applied to item selection (see the study-aids
> note below for `preferred_item_type`, which now is) — the plan drives *which* KC gets
> practiced, not yet *what difficulty* it's practiced at; `subject_id` is set API-first with no
> caller yet (no frontend exists before Phase 7) — a future "continue subject X" entry point is
> expected to populate it, same bootstrapping pattern every other Phase 5 endpoint has shipped
> under.
>
> **Study aids landed.** No new mutation surface here either — this slice only widens *what*
> `session_runner` can serve. `app/learning/item_generation.py` gained
> `generate_fill_blank_item`/`generate_flashcard_item` (same FAST-role JSON-prompt shape as the
> existing MCQ generator) plus a `GENERATORS` dispatch map. `session_runner.item_for_kc` replaces
> the previous slice's untyped reuse-then-generate-MCQ with an order that closes the
> `preferred_item_type` gap flagged above: reuse a bank item of the plan's preferred type (a free
> win even for types with no generator, e.g. `cloze`) → generate that type if a generator exists
> → reuse any type → generate MCQ. Steps with no explicit preference default to `"new"` → no
> preference, `"review"` → flashcard, pairing the roadmap's "flashcards" and "spaced-repetition
> surfacing" into one default. `GET /reviews/due` now resolves an answerable flashcard per due KC
> (`session_runner.due_review_items`) instead of returning KC metadata alone — a
> plan-independent review queue, since clearing a spaced-repetition backlog shouldn't require
> being mid-lesson. Resolution is capped at `reviews_due_item_limit` (soonest-due first); the due
> list itself is never truncated. This is the first GET endpoint in the codebase with a
> generation side effect (every other generation call site sits behind a POST) — accepted on the
> same reuse-then-generate-forever economics as everywhere else, now bounded by that cap. **Known
> v1 gaps:** flashcards store the generated answer (`answer_key={"back": ...}`) but nothing
> reveals it to the learner before self-rating yet — self-graded recall is fully trust-the-learner,
> a pre-existing gap this slice scales into the default review experience rather than
> introduces; cloze has no generator (only fill-in-the-blank does), so a profile preference for
> cloze can only ever be served from the bank, never freshly generated; `target_difficulty`
> remains unapplied.
>
> **Memory landed.** Greenfield — `app/memory/` (extraction + vector-only retrieval) plus
> `app/services/memory.py` (write-back/dedup/view/erase), mirroring the closest existing analogs
> end-to-end rather than inventing new patterns: `Memory(learner_id, conversation_id, kind,
> content, embedding)` mirrors `Chunk`'s `Vector(768)` + HNSW shape (no `tsv` column — the
> TECHNICAL_DESIGN §7.1 schema sketch omits one, so retrieval is vector-only for v1, unlike
> RAG's hybrid RRF); `extract_memories` (FAST role, JSON-only) mirrors `kc_tagging.py`'s
> tolerant-parse-or-empty idiom. **Retrieval is what "wires memory into sessions"**: every tutor
> turn embeds the learner's message and folds the top `memory_retrieval_limit` hits into the
> system prompt as a third context note (after goal and plan-grounding), learner-global rather
> than gated behind `subject_id`. **Write-back is separate and on-demand**
> (`POST /conversations/{id}/memory/write-back`, 202, queued via `memory_write_back_task` —
> mirrors the ingestion enqueuer seam byte-for-byte) rather than firing per-turn, since
> extraction costs a real FAST call for no accumulated benefit if run on every message (same
> reasoning as `POST /profile/refresh` staying on-demand). Candidate memories are deduped before
> persisting via cosine distance to an existing same-`(learner, kind)` memory
> (`memory_dedup_max_distance`), reusing the storage primitive rather than a new subsystem —
> meaningfully better than exact-string dedup, since wording drift across extraction calls would
> make that fire almost never. `GET /memory` (view) and both `DELETE /memory/{id}` and bulk
> `DELETE /memory` (erase) ship now rather than later: the NFR's "learner-controllable
> view/reset" bar, stated about the profile, applies at least as strongly to discrete personal
> facts. Verified live end-to-end against real Postgres + Ollama: a fact revealed in one
> conversation, written back, then surfaced unprompted in a *second*, unrelated conversation's
> system prompt — the literal Phase 5 DoD bar. **Known v1 gaps:** deletion has no tombstone — a
> later write-back over overlapping conversation history can re-extract a fact the learner just
> deleted (the capped extraction window ages the overlap out over time, but doesn't prevent it);
> no `source` column distinguishing extraction origin (YAGNI — every row comes from conversation
> extraction today; `conversation_id`'s nullability is the future signal if a second source
> shows up). This closes out Phase 5 — every scope item above is now landed.

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
