# Guru — Technical Design

> **Status:** Living engineering supplement to [MASTERPLAN.md](./MASTERPLAN.md) (the *what & why*)
> and [ROADMAP.md](./ROADMAP.md) (the *order*). This document is the *how*: concrete interfaces,
> data model, the LLM stack, orchestration, ingestion, and the learning engine. Code sketches are
> **illustrative** — they fix intent and shape, not final signatures. When an approach here changes,
> update this file alongside the code.

---

## 1. Stack & Conventions

- **Language/runtime:** Python 3.13, managed by **uv**.
- **Web:** FastAPI (async), Uvicorn, SSE for streaming.
- **Data:** PostgreSQL (pgvector HNSW, pg_trgm/GIN, tsvector); SQLAlchemy 2.0 async over `asyncpg`;
  Alembic migrations.
- **Types:** Pydantic v2 at boundaries; **ty** (static) + **beartype** (runtime, dev/test via
  `beartype.claw` on first-party packages only — third-party LLM libs are excluded to avoid friction).
- **Quality:** ruff (lint+format), pytest (+pytest-asyncio), orchestrated by **poethepoet**.
  `poe check` = lint + type-check + test is the green gate every phase must pass.
- **Config:** `pydantic-settings`, env-profiled (`dev` / `test` / `prod`); secrets from env, never
  committed. `.env.example` documents every key.

**Layering rule:** `api → services → (llm | rag | agent | memory | learning) → models/db`. Routers
stay thin; no router or service ever imports a provider SDK directly — all model access goes through
`app/llm`. Business logic never imports FastAPI.

---

## 2. Module Layout

```text
app/
  core/        config, async engine/session, logging, deps, auth-stub, beartype claw
  api/v1/      routers (thin); SSE endpoints
  schemas/     Pydantic request/response
  models/      SQLAlchemy ORM
  services/    orchestration of the modules below into use-cases
  llm/         provider abstraction, model-role registry, providers, cost accounting
  prompts/     DSPy modules/signatures + the interactive refinement gate
  agent/       LangGraph graphs (tutoring, lesson-gen, grading, content-assembly), tools
  rag/         ingestion adapters, normalization, chunking, embedding, hybrid retrieval
  memory/      per-learner long-term memory + summarization + write-back
  learning/    knowledge graph, KnowledgeTracer, aggregation, FSRS, learner profile,
               lesson-plan policy, assessment+grading, content generation/assembly, analytics
  workers/     background tasks (taskiq/arq) entrypoints
db/migrations/ Alembic
tests/         unit (FakeProvider) + integration (Ollama, test DB) + eval
```

---

## 3. LLM Stack

### 3.1 Provider abstraction

A minimal, provider-agnostic surface. Everything else builds on it.

```python
class LLMProvider(Protocol):
    async def complete(self, req: ChatRequest) -> ChatResponse: ...
    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]: ...
    async def embed(self, texts: list[str], *, model: str) -> list[Vector]: ...
    # tool-calling is expressed in ChatRequest.tools / ChatResponse.tool_calls
```

`ChatRequest`/`ChatResponse` use **one unified message + tool schema** (role, content parts incl.
images for vision, tool calls/results, usage). Providers translate to/from their native formats so no
provider shape leaks upward.

**Providers:** `OllamaProvider` (local dev), `OpenRouterProvider` (OpenAI-compatible; first prod
integration), `AnthropicProvider` (direct), later `Bedrock/Vertex/Azure`; `FakeProvider` (scripted,
deterministic, offline) for unit tests.

### 3.2 Model-role registry (roles, not models)

Application code asks for a **role**, never a model name.

```python
class ModelRole(StrEnum):
    FAST = "fast"      # tagging, routing, classification, the refinement gate
    SMART = "smart"    # tutoring, grading, most generation
    GENIUS = "genius"  # hard reasoning, curriculum/graph synthesis
    EMBED = "embed"    # vectorization

# registry.client_for(ModelRole.SMART) -> a ready client bound to the env's (provider, model)
```

The role→`(provider, model, params)` mapping is **config, per environment**:

| Role | dev/test | prod (example) |
| ---- | -------- | -------------- |
| `FAST` | Ollama `gemma`/`qwen` (local) | cheap OSS / `claude-haiku-4-5` |
| `SMART` | OpenRouter cheap mid-model | `claude-sonnet-4-6` |
| `GENIUS` | OpenRouter cheap (sparingly) | `claude-opus-4-8` |
| `EMBED` | local embed (e.g. via Ollama) | cloud embeddings |

Rationale recap: tests are token-heavy, so dev runs `FAST` locally on Ollama for free and routes
`SMART`/`GENIUS` to a cheap cloud model (100B-class models don't run usefully on a laptop). Swapping
any model is a config edit — zero code change. Concrete names above (`gemma`, `qwen`, etc.) are
placeholders set in config.

### 3.3 LangGraph compatibility & cost

LangGraph nodes consume models **by role through our existing `LLMClient`** — our client
*is* the adapter; we do not introduce LangChain chat models. Nodes are plain async functions
that call `llm.stream(ModelRole.SMART, …)` / `llm.complete(…)`, so graphs stay
provider-agnostic and there is a single model abstraction. Token streaming is surfaced with
LangGraph's **custom stream writer** (`get_stream_writer()`; `stream_mode="custom"`), not
`stream_mode="messages"`. Every call records `usage` + computed cost, tagged by `(role,
model, request_id, graph_node)`, into a `llm_calls` log → cost dashboards.

---

## 4. Orchestration (LangGraph)

LangGraph is the substrate for any multi-step LLM flow. Each flow is a typed **state graph** with
nodes (LLM calls, tools, retrieval, tracer updates), conditional edges (branch/retry), and
checkpointing for **human-in-the-loop (HITL)** interrupts.

**Core graphs:**

- **Tutoring turn** — refine? → retrieve context (RAG + memory) → generate (SMART) → stream → extract
  observations → update tracer → persist.
- **Interactive refinement gate** (§5.1) — HITL loop that co-constructs the goal/prompt.
- **Lesson generation** — objectives → prerequisite-ordered KCs (GENIUS for synthesis) → assemble
  content blocks → personalize framing.
- **Grading** — item + response → auto-grade or LLM rubric-grade → graded observation → tracer.
- **Content assembly** — fetch reusable KC blocks → re-sequence/scaffold/re-frame for the learner.

**State & persistence:** graph state must be checkpoint-serializable (for HITL interrupts),
so the `AsyncSession` never lives in state — turn persistence (user/assistant messages,
`LLMCall`) is orchestrated in a thin service wrapper (`run_tutor_turn`) around the graph.
Nodes that need the session later (e.g. tracer-update) receive it via LangGraph runtime
context, not state.

**Streaming:** node token streams are surfaced over **SSE** to the client; HITL interrupts surface as
events the client answers, resuming the graph from its checkpoint.

**Introduction timing:** Phases 2–3 use direct registry calls behind the same service seams; graphs
are introduced in Phase 5 when genuine branching/HITL/workflows appear (avoids premature LangGraph
ceremony, honors YAGNI).

---

## 5. Prompt Quality

Two distinct layers — keep them separate.

### 5.1 Interactive refinement gate (runtime, HITL)

The learner rarely supplies the optimal prompt. The gate is a LangGraph subgraph that **loops with
the learner until they are satisfied**, then commits to (potentially expensive) generation:

```text
extract intent ─▶ propose refined goal/prompt ─▶ ask learner ──satisfied?──▶ commit
       ▲                                              │ no
       └──────────── incorporate feedback ◀───────────┘
```

It runs on `FAST`/`SMART`, captures ambiguities, adds pedagogical framing, and records the agreed
goal. Pedagogically it doubles as a placement/metacognition signal (what the learner thinks they want
vs. what they need). Exit conditions: explicit learner acceptance, or a max-rounds fallback.

### 5.2 DSPy (offline optimization)

DSPy treats our LLM steps as **modules with signatures** and **compiles** their prompts/few-shots
against eval metrics + datasets (drawn from the KC-tagged event log). It optimizes our own modules —
grading, the refinement gate, content generation — and we deploy the compiled prompts. It is a
build/optimization-time tool, gated by eval deltas (Phase 8), not a runtime dependency on the hot
path.

---

## 6. RAG & Multimodal Ingestion

### 6.1 Ingestion pipeline

All sources converge on one pipeline, run as background jobs (§9):

```text
source ─▶ adapter (extract) ─▶ normalize ─▶ chunk ─▶ embed (EMBED) ─▶ store (pgvector + provenance)
```

**Source adapters:**

| Source | Approach |
| ------ | -------- |
| PDF / DOCX / PPTX / XLSX / TXT | text+structure extraction (e.g. PyMuPDF, python-docx/pptx, openpyxl; `unstructured`/`docling` as umbrella) |
| Handwritten / scanned notes | **vision-LLM OCR** (best on handwriting); printed text may fall back to Tesseract |
| Audio / video | **ASR** (Whisper / faster-whisper); video → demux audio (+ optional keyframes for slides) |
| Public weblinks | fetch + readability extraction (e.g. trafilatura); respect robots; static first, dynamic later |

Every chunk carries **provenance** (source id, locator — page/timestamp/url, extraction method,
confidence) for citations and the privacy/compliance gate.

### 6.2 Retrieval

**Hybrid:** pgvector (HNSW, cosine) for semantic similarity + Postgres `tsvector`/GIN for keyword +
metadata filters (KC, source, learner scope). Results fused (e.g. reciprocal-rank fusion); optional
re-rank later. Retrieval is always **scoped** (by guru/KC/learner) and returns provenance for
grounded, citable generation.

---

## 7. The Learning Engine

### 7.1 Data model (core tables)

```text
subjects, topics, kcs                      # knowledge graph nodes
kc_edges(prereq_kc_id, kc_id, weight)      # prerequisite DAG
content_blocks(kc_ids[], type, body, ...)  # reusable lessons/wikis/questions, KC-tagged, cached
items(kc_ids[], type, stem, key, rubric_id)# assessment items (MCQ/cloze/short/long/flashcard)
rubrics(kc_id, criteria)                   # per-KC grading rubrics
learners                                   # stub identity for MVP
learner_kc_state(learner_id, kc_id, ability, uncertainty, last_seen, due_at)
learner_profiles(learner_id, created_at, updated_at)               # the "how they learn" model
profile_dimensions(learner_id, key, value jsonb, uncertainty,      # one row per dimension
                   kind, source, updated_at)  # kind=trait|state, source=behavioral|self_report
learning_events(...)                       # immutable, replayable, KC-tagged (see 7.5)
lesson_plans(learner_id, goal, steps[])    # adaptive teaching policy
conversations, messages
sources, chunks(embedding vector, tsv, provenance jsonb)
memory(learner_id, kind, content, embedding)
llm_calls(request_id, role, model, usage, cost, node)
```

### 7.2 KnowledgeTracer interface (swappable)

```python
class KnowledgeTracer(Protocol):
    def estimate(self, learner_id, kc_id) -> Estimate:        # (ability, uncertainty)
    def update(self, obs: Observation) -> None:               # one graded interaction
    def due_reviews(self, learner_id) -> list[ReviewItem]:    # via FSRS
```

`Observation` carries `learner_id`, `kc_ids` (+weights for multi-KC items), `score ∈ [0,1]` (partial
credit), `item difficulty`, latency, hints. Implementations are swappable (Elo/Glicko now → DKT
later) and may be ensembled.

### 7.3 Continuous estimator (Elo/Glicko-style) — baseline

Per-KC continuous ability `θ` and per-item difficulty `d`; treat each interaction as a "match":

```text
expected = 1 / (1 + 10^((d - θ)/400))          # logistic expectation
θ        += K * (score - expected)             # score ∈ [0,1] supports partial credit
d        -= K_item * (score - expected)        # items drift; or calibrate via IRT offline
```

Use the **Glicko/TrueSkill** variant to carry an **uncertainty** (rating deviation) that shrinks with
evidence and grows with elapsed time — driving "assess more here vs. trust this." These formulas are
the **starting point** to validate empirically behind the interface, not a contract.

### 7.4 Hierarchical roll-up

KC ability aggregates upward to topic and subject as a **weighted mean** (weights from edge
importance/coverage), with uncertainty propagated (e.g. weighted variance); untested KCs widen a
node's uncertainty. **Multi-KC items** apportion the update across their tagged KCs by weight (an
additive approximation; PFA/AFM is the principled upgrade later). The dashboard reads every level so a
learner drills from "Calculus 62% (wide)" into "Integrals 40%, integration-by-parts weakest."

### 7.5 Event log = DKT's training set

Every interaction appends an immutable `learning_event`: `(learner_id, kc_ids, item_id, response,
score, latency, hints, model_meta, ts)`. This replayable, KC-tagged log is exactly what **DKT** needs
later; we earn it passively while Elo/Glicko runs. Swapping in DKT touches only a `KnowledgeTracer`
implementation.

### 7.6 Assessment & grading

Auto-gradable items (MCQ/cloze/fill-in-blank) grade deterministically. Open responses are graded by
an **LLM rubric** (SMART) against the item's per-KC rubric into a **graded score**; calibrated
placement/unit tests use full **IRT (2PL/GRM)**. All paths emit an `Observation`.

### 7.7 Lesson-plan policy

Given goal + learner model (mastery **and** profile, §7.8), generate objectives →
**prerequisite-ordered KCs** (topo-sort over the DAG, gated by mastery+uncertainty) → steps with
**tier-scaled scaffolding**. The plan is followed by the session runner and **revised on evidence**
(mastery gains, struggles, FSRS due reviews, profile shifts) — an adaptive policy, not a static
document.

### 7.8 Learner profile (the *how*)

A multi-dimensional, **behavior-derived** model of how a learner learns — sibling to the tracer,
behind its own swappable interface. Evidence-based dimensions only; modality preference is captured
as a UX/engagement signal, not a mastery lever (rationale: MASTERPLAN §4.8).

```python
class LearnerProfile(Protocol):
    def get(self, learner_id, dim: DimensionKey) -> DimensionValue:   # value + uncertainty
    def update(self, learner_id, signals: Sequence[Signal]) -> None:  # from events/telemetry
    def snapshot(self, learner_id) -> dict[DimensionKey, DimensionValue]  # for policy + dashboard
```

- **Dimensions** (one `profile_dimensions` row each), across four families: *cognitive & pace* (pace,
  optimal-challenge band, error-type, load tolerance), *metacognition* (calibration, help-seeking,
  persistence), *motivation & affect* (goal orientation, engagement/flow, confidence), *context &
  preferences* (interests, representation engagement, reading level/accessibility, session logistics).
- **Each value** has `uncertainty`, a `kind` — **trait** (slow-moving; updated via EWMA) vs **state**
  (dynamic; recent-window/session-scoped) — and a `source` (behavioral vs self-report).
- **Estimation is behavior-first.** Per-dimension estimators consume the same `learning_events` log +
  session telemetry: pace from rolling latency/throughput; error-type via a `FAST`-model classifier
  over wrong answers; calibration by comparing self-prediction prompts to outcomes; affect via
  heuristics (error streaks, pace collapse) + optional `FAST` signal. **Cold-start:** a short optional
  intake + signals from the refinement gate + placement seed values at high uncertainty; behavioral
  evidence dominates as it accrues.
- **Consumption.** The lesson-plan policy and content-assembly graph read a `snapshot`: pace/challenge
  → step size + desirable-difficulty item targeting; error-type → remediation/feedback style;
  help-seeking/persistence → hint policy + intervention timing; interests → example/analogy selection;
  reading level → language complexity; representation engagement → format/UX (always offer
  multi-representation).
- **Learner-facing & privacy.** Surfaced on the dashboard ("how you learn"); the learner can view and
  **reset** dimensions. Sensitive data — in scope for the privacy gate (MASTERPLAN §8).

---

## 8. API, Auth, Observability

- **API:** versioned `/api/v1`; Pydantic schemas distinct from ORM; SSE for streaming + HITL events;
  consistent error envelope; cursor pagination. OpenAPI schema drives the typed frontend client.
- **Auth (stubbed):** `get_current_learner` dependency returns a dev learner (or reads a header);
  `learner_id` is threaded through every layer **now** so real auth (JWT/OAuth/managed) later is a
  dependency swap, not a refactor.
- **Observability:** structured logs + request IDs; LLM/tool tracing; per-call token+cost
  (`llm_calls`); an **eval harness** for educational correctness + grading reliability treated as a
  release gate (wrong teaching is worse than none).

---

## 9. Background Work & Deployment

- **Jobs:** FastAPI `BackgroundTasks` early; a real queue (**taskiq** or **arq** + **Redis**) from
  Phase 4 for ingestion, content pre-generation, memory write-back, analytics. Jobs are idempotent
  and resumable.
- **Dev:** `docker compose` brings up Postgres+pgvector, Redis, and (optionally) Ollama. `FAST` runs
  locally; `SMART`/`GENIUS` hit OpenRouter with a dev key.
- **Prod:** OpenRouter as the first inference integration (broad, swappable); a hyperscaler
  (Bedrock/Vertex/Azure) added later for data-residency/compliance. Stateless API behind the queue +
  workers; the role registry makes prod model choices configuration.

---

## 10. Testing

- **Unit:** pure logic + services against `FakeProvider` — fast, offline, deterministic (CI default).
- **Tracer:** property tests (mastery rises with correct answers, uncertainty shrinks with evidence,
  roll-up monotonicity).
- **Integration:** real Postgres (transactional rollback per test); **Ollama** for realistic local
  generation without token cost.
- **Eval:** golden/eval sets for grading reliability and content correctness; DSPy optimizes against
  these and they gate prompt changes.

---

## 11. Dependency Introduction Timing

Heavy frameworks earn their place; introduce them at the phase that needs them, behind thin seams:

| Dependency | Enters at | Why then |
| ---------- | --------- | -------- |
| Ollama / OpenRouter + role registry | Phase 2 | first real model calls |
| Redis + taskiq/arq | Phase 4 | ingestion/pre-generation need async jobs |
| LangGraph | Phase 5 | first branching/HITL/multi-step workflows |
| FSRS lib | Phase 3 | retention scheduling in the loop |
| DSPy | Phase 8 (data-dependent) | needs eval datasets from the event log |
| Vision-LLM OCR / Whisper | Phase 4b | heavier ingestion modalities |
