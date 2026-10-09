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

Cross-cutting: SSE for token streaming · `learner_id` threaded everywhere behind the **identity
seam** (`app/core/identity.py`; Clerk in prod, a fake in tests) · **token/cost logged per LLM
call** (tagged by role+model) from day one · Redis-backed job
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
- **Identity is hosted (Clerk), authorization is ours** (S21) — Clerk owns passwords, recovery
  and social sign-in; Guru keeps invitations, the admin tier, suspension and audited visits. One
  module imports the SDK; a token is exchanged once for the existing session cookie, so nothing
  downstream knows Clerk exists. See [docs/RUNBOOK.md](docs/RUNBOOK.md) §11.
- **Learner material is private; sharing is reviewed** (S25) — a subject is either one learner's
  own (`owner_learner_id`) or curated (NULL), and `is_visible_to`/`is_writable_by` in
  `app/services/knowledge.py` are the whole authorization model for the graph. Publishing copies
  a frozen snapshot an administrator approved; it never makes the original public, and a subject
  built from the learner's uploads can never be published at all. See
  [docs/RUNBOOK.md](docs/RUNBOOK.md) §12.
- **One source scope, one grounding policy** (S26/S28) — `resolve_scope` in `app/rag/scope.py`
  decides what any generation may read (a subject's own sources; untagged ones only if the
  subject opts in; a General chat reads none), and `app/services/grounding.py` decides what the
  tutor is told, including sources-only and "nothing matched". The coverage label on a reply is
  derived from what was offered and cited, never from the model's own account.
- **A citation outlives a re-ingest** (S29/S50) — re-ingesting supersedes chunks rather than
  deleting them when something cites them, and re-processing a finished source is the
  learner's confirmed decision. `uv run poe reindex` re-embeds in place (ids kept) and only
  re-extracts when asked; staleness is read from the chunks, so a run resumes by running again.
  See [docs/RUNBOOK.md](docs/RUNBOOK.md) §15. Ingestion runs in committed stages (extract →
  embed → publish → tag) that resume where they stopped, under exact global and per-learner
  slots (S37).
- **Archive, delete and forget are three actions** (S61, S42; V11) — archive is reversible and
  out of use (retrieval drops archived sources, archived conversations are read-only); delete is
  immediate after an impact report of what stays; forget removes only what was derived — lessons
  built on a source, memories learned in a conversation — never answers or mastery.
  `app/services/removal.py` is the one place that decides what derives from what.
  Forgetting a conversation suppresses its facts only from that conversation; a replaced memory
  is an explicit judgement (`app/memory/supersession.py`), shown and undoable.
- **Deleting an account is a state, then an erase** (S61; V12) — access ends at the request
  and every session is revoked; signing in again within seven days reaches only the recovery
  routes (`AccountHolder`); then a worker erases every store and the identity provider's copy.
  What the object store or provider refuses becomes a `pending_erasures` row retried until done.
  Diagnostic rows keep nothing pointing at a learner past 30 days. Paused practice state is
  erased with its conversation, onboarding session or account, and a deploy that changes a
  graph drops (never resumes) the old shape's paused state (S17). See
  [docs/RUNBOOK.md](docs/RUNBOOK.md) §16.
- **An explicit setting pins; inference adapts only what is left to it** (S02; V09) — five
  settings (guidance, explanation level, note format, hints, pace), global with subject
  overrides, resolved in one place (`app/services/preferences.py`) and applied when
  instructions are assembled, so a change reaches the next turn (in guided practice, the next
  question). Inferred values are still
  computed and shown beside the setting; only catalog strings ever reach a prompt.
- **Background work runs when things go quiet** (S43) — memory write-back and profile refresh
  are queued by a worker sweep (`app/services/refresh_schedule.py`) for conversations and
  learners with unread evidence and no activity for 20 minutes; due-ness is derived from the
  data, so backlogs catch up by themselves. The profile reads a recency window, and a
  model-backed estimator pays only when its input changed. Learners can pause memory, which
  also stops the profile reading what they type, then and afterwards (O07).
- **Every paid call is recorded and admitted by the client** (S47, S48) — `LLMClient` writes a
  `pending` row before each call and settles it (`ok`/`failed`/`partial`);
  `app/services/spend_guard.py` refuses a call over the learner's daily caps (exact under
  concurrency) or the deployment ceiling, and background work stops at 90%. Services say what
  they are with `@metered(...)` (`app/llm/attribution.py`); nothing calls a logging function by
  hand. A streamed turn has a deadline and the learner can stop it, keeping its text
  (`app/services/turn_control.py`); any other request answers 504 past its own. A provider
  that is busy or down is a refusal too (`CallRefused`): 503, or a coded turn error, never
  "generation failed" (S49). See
  [docs/RUNBOOK.md](docs/RUNBOOK.md) §18.
- **A grade says what measured it** (S56) — every graded event carries a `grading` block
  (grader, model, and hashes of frozen item/rubric/prompt snapshots in `grading_snapshots`, per
  learner, surviving item deletion); `uv run poe regrade` re-grades past answers under a current
  or recorded grader and reports agreement, never changing a grade. See
  [docs/RUNBOOK.md](docs/RUNBOOK.md) §19.
- **A self-rating is not evidence of ability** (S56) — the server decides whether a score was
  judged or self-reported, and a self-rating moves the review schedule only. Mastery is the
  conservative estimate `ability − 2·uncertainty ≥ 0.5` on measured evidence (V02); achievement
  is recorded per component and kept, and staleness is reported, never used to un-master. An
  answer given straight after a worked example is not unaided, and a component owed a second
  unaided answer gets a cold retention check in its review queue (S14). Transfer is an unaided
  correct answer in a catalogue setting (`app/learning/transfer.py`) no earlier attempt used,
  checked after retention; it is shown as evidence and not required for achievement.
- **A cross-subject link needs two agreements** (S24) — an endorsement (admin for curated
  pairs, the `SMART` judge for private ones) and the learner's own acceptance. A shared name
  never links anything, and an accepted link gives only a provisional head start that real
  answers must confirm. `links_in_effect` in `app/services/concept_links.py` is the one rule;
  see [docs/RUNBOOK.md](docs/RUNBOOK.md) §13.
- **Jev is a first pass, never an author** (S78–S83) — TypeSafe's System One model answers
  typed questions about a turn (`intent`, `fully_correct`) in front of the FAST gate and the
  SMART grader. A confident live answer may skip that model call; it never writes text and
  never produces a failing grade. Each question is off / shadow / live on its own and goes live
  only after a person reads `uv run poe decision-report`. `app/llm/decisions.py` is the only
  SDK importer. See [docs/RUNBOOK.md](docs/RUNBOOK.md) §14.
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
