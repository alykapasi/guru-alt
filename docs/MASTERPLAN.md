# Guru — Masterplan

> **Status:** Living north-star document. It captures *what* we are building and *why*, and the
> decisions that should stay stable across many phases. Implementation sequencing lives in
> [ROADMAP.md](./ROADMAP.md); deeper engineering detail lives in
> [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md). When a decision here changes, update this file first.

---

## 1. Vision & Mission

**Guru is an AI-first personalized learning platform.** Its single promise is **durable learning** —
knowledge that *sticks* rather than passing through the mind. Every learner gets a non-fungible
experience: content, pacing, scaffolding, and assessment adapted to how *they* learn — their level,
habits, strengths, and weaknesses.

**The mission is to raise the educational floor.** Guru is the consumer wedge for a larger goal:
partner with schools and governments to enable a **flipped-classroom model** where students learn
core material on Guru, and human class time becomes consolidation and personalized help. We want the
least-served learners to get the basic tools to compete.

*"alt" is a version suffix for the build currently in progress, not part of the product name.*

---

## 2. Product Principles

1. **Durable over delivered.** Success is measured by retention and transfer, not content consumed.
   The system optimizes for knowledge that survives the forgetting curve.
2. **Non-fungible experience, reusable production.** Every learner's *experience* is unique, but it
   is assembled and re-framed from reusable building blocks — uniqueness lives in sequencing,
   scaffolding, and framing, not in regenerating everything from scratch. (See §5.4.)
3. **Scaffolding scales inversely with level.** Advanced learners need less abstraction and more
   Socratic challenge; younger learners need more structure, modeling, and guidance.
4. **Metacognition is part of the product.** Learners should see *how* they learn. Surfacing the
   model builds ownership, motivation, and self-regulated learning.
5. **Raise the floor.** When trade-offs appear, favor accessibility, cost-efficiency, and reach for
   the underserved over polish for the few.

---

## 3. Personas & Tier Rollout

The platform spans all education levels, rolled out **top-down** — highest abstraction tolerance
first, because those learners need the least scaffolding and the system can learn on them safely
before serving children.

| Order | Tier | Notes |
| ----- | ---- | ----- |
| 1 (**MVP**) | Adult / intellectual self-directed | Self-motivated; "teach me X." Lowest scaffolding; no child-privacy regime. |
| 2 | College (associates / bachelors / masters) | Structured subjects; still self-directed. |
| 3 | High school | Standards begin to matter; school/govt partnerships start. |
| 4 | Middle school | More scaffolding, more guardrails. |
| 5 | Elementary | High scaffolding, multimodal, gamified. |
| 6 | Pre-K | Maximum scaffolding; heavy multimodal; guardian-mediated. |

**MVP persona:** an adult self-directed learner. **User type for MVP:** end-user/consumer only.
Guru-creator, teacher, and org-admin roles are deferred (see §8).

> **Hard gate:** Any tier below adult triggers child-data and education-privacy obligations
> (COPPA, FERPA, GDPR-K). These must be satisfied *before* that tier ships — they are not retrofit
> work. The MVP deliberately stays in the adult tier to avoid this until the architecture is ready.

---

## 4. The Learning Engine (core of the product)

This is the heart of Guru and the part most worth getting right. It has several cooperating parts.
The complete **learner model** has two halves: the **mastery model** (§4.2 — *what* a learner knows)
and the **learner profile** (§4.8 — *how* they learn); they feed the lesson-plan policy together.

### 4.1 Knowledge graph (the backbone)

Everything hangs off a structured model of knowledge:

```text
Subject  ──contains──▶  Topic  ──contains──▶  KC (knowledge component / leaf skill)
   └────────────────── prerequisite edges (DAG) ──────────────────┘
```

- **KC** = the smallest unit of "knowing" we track (e.g., within Calculus: *limits*,
  *u-substitution*, *integration by parts*, *convergence tests*).
- **Prerequisite edges** form a DAG used for sequencing and for propagating evidence
  (struggling with derivatives flags a possible gap in limits).
- For the MVP, the graph is **AI-generated** on demand for a learner's goal; the LLM tags every
  lesson and item with its KC(s) at generation time. This bootstraps the labeled data later models
  need. Curated and standards-aligned graphs come later (§8).

### 4.2 Mastery model — continuous, hierarchical (the *what*)

We model **per-KC ability as a continuous value with uncertainty**, updated online. This is the
*what they know* half of the learner model; the *how they learn* half is the profile (§4.8).

- **Why not binary BKT.** Bayesian Knowledge Tracing models a *binary* latent state
  (mastered / not). Its output probability means "confidence they are in the mastered state," **not
  "degree of understanding,"** and its observations are binary (right/wrong). For a platform whose
  whole value is fine-grained personalization, collapsing partial understanding to true/false is
  unacceptable information loss.
- **Spine: dynamic IRT (Elo/Glicko-style).** Each learner carries a continuous **ability per KC**;
  each item carries a continuous **difficulty**; both update after every interaction (streaming
  1PL-IRT). It is continuous, low-data, cheap, online, and naturally produces the rich numbers the
  dashboard needs. The **Glicko/TrueSkill** variant adds an **uncertainty** band — telling the
  planner where it must assess more vs. where mastery is solid.
- **Hierarchical roll-up.** A single subject score is itself lossy. Ability is tracked at the **leaf
  KC** level and **aggregated upward** — weighted by importance/coverage, with uncertainty
  propagated — into topic and subject scores. The dashboard lets a learner drill from
  "Calculus 62%" into "Integrals 40% — weak on integration-by-parts."
- **Multi-KC items.** An item may touch several KCs (a hard integral tests substitution + algebra);
  credit/blame is apportioned across its tags.
- **Calibrated measurement.** Full **IRT (2PL / Graded Response Model)** is reserved for moments that
  need precise measurement — placement diagnostics and unit tests — while the Elo-style tracer
  handles continuous day-to-day tracing.

### 4.3 Retention — forgetting is a separate problem

Mastery ("do they understand it") and retention ("when will they forget it") are different.
**FSRS** (modern spaced-repetition scheduler) models memory decay and schedules reviews on top of
the ability model. Together: the tracer says *what is understood*; FSRS says *what needs reviewing
and when*.

### 4.4 Assessment & grading

- **Item types:** MCQ, cloze / fill-in-the-blank, short-form, long-form, plus flashcards.
- **Auto-graded** items (MCQ/cloze/fill-in-blank) grade deterministically.
- **LLM rubric grading from v1:** open short/long responses are graded against per-KC rubrics into a
  **graded (partial-credit) score**. This is the concrete cure for binary information loss — a
  half-right answer moves the needle halfway (IRT's Graded Response Model consumes exactly this).
- Every graded outcome becomes an **observation** that updates the tracer.

### 4.5 The `KnowledgeTracer` seam & the path to DKT

- All estimation lives behind a stable interface: roughly
  `estimate(learner, kc) -> (ability, uncertainty)`, `update(observation)`, `due_reviews(learner)`.
- **Every interaction is logged as a KC-tagged, replayable event** (learner, KC(s), item, response,
  correctness/score, latency, hints used, timestamp) from day one. That event log *is* the labeled
  training set for **Deep Knowledge Tracing** later. We earn it passively while the Elo/IRT tracer
  runs, and we can A/B or ensemble estimators (even add BKT's guess/slip signal) without touching the
  planner, tools, or UI.

### 4.6 The lesson plan — an adaptive teaching policy

The lesson plan is **not a static document**; it is a teaching *policy*, analogous to a Claude Code
plan. It is **generated** against the learner model + target objectives (objectives → prerequisite-
ordered KCs, with scaffolding chosen by tier), **followed** during sessions, and **revised** as
evidence arrives. Higher tiers get terser, more Socratic plans; lower tiers get more steps, modeling,
and checks for understanding.

### 4.7 The core learning loop

```text
place ─▶ goal ─▶ generate lesson plan ─▶ session ─▶ assess ─▶ update tracer ─▶ adapt ─▶ (repeat)
  │                                                              │
  │  diagnostic + inference + just asking → seed KC priors        │  ability+uncertainty updated,
  │                                                              │  rolled up, FSRS schedules
  └──────────────────────── dashboard reflects state ◀───────────┘  reviews, event logged
```

### 4.8 Learner profile — how they learn (the *how*)

Where the mastery model tracks *what* a learner knows, the **learner profile** is a multi-dimensional,
**behavior-derived** model of *how* they learn. It feeds the lesson-plan policy and content assembly
alongside mastery.

- **Evidence-based, not "learning styles."** We deliberately reject the popular VARK / modality-
  matching theory (visual/auditory/kinesthetic): matching instruction to a preferred modality has **no
  replicated effect on learning outcomes**. We model dimensions that *demonstrably* shape learning and
  that we can *observe*. Representation/modality preference is still captured, but used **only as an
  engagement/UX signal** (and we always offer multi-representation, which helps everyone) — never as a
  mastery lever.
- **Four dimension families.** *Cognitive & pace* (pace, optimal-challenge / desirable-difficulty
  band, error-type profile — conceptual vs procedural vs careless, cognitive-load tolerance);
  *Metacognition & self-regulation* (self-assessment calibration, help-/hint-seeking behavior,
  persistence); *Motivation & affect* (goal orientation, engagement/flow vs frustration/boredom,
  confidence); *Context & preferences* (interests for grounding examples, representation engagement,
  reading level / accessibility, session logistics).
- **Each dimension** carries a value, an **uncertainty** (shrinks with evidence, like the tracer), a
  **trait-vs-state** kind (traits move slowly; affect/engagement are dynamic), and a **source**
  (behavioral vs self-reported). Estimators are swappable and the dimension set is extensible.
- **Behavior-first.** Dimensions are inferred from the same KC-tagged event log + session telemetry; a
  short optional intake, the interactive refinement gate, and placement seed the cold-start at high
  uncertainty, then behavior takes over.
- **The payoff.** pace/challenge → step size & item targeting; error-type → remediation/feedback;
  help-seeking/persistence → hint policy & intervention timing; calibration → self-prediction prompts;
  goal-orientation/affect → framing & difficulty to sustain flow; interests → example selection;
  reading level → language complexity. The profile is also **surfaced to the learner** ("how you
  learn") for metacognition. It is distinct from **memory** (§5): memory stores facts ("likes
  basketball"); the profile quantifies learning traits and *uses* such facts.

---

## 5. Domain Model (concepts)

- **Learner** — an end user. Owns a learner model (mastery + profile), memory, goals, and event
  history. Auth identity is **stubbed** for MVP behind a clean seam (§7).
- **Knowledge graph** — Subjects, Topics, KCs, prerequisite edges (§4.1).
- **LearnerKCState** — continuous ability + uncertainty + review schedule per (learner, KC).
- **Learner profile** — multi-dimensional, behavior-derived traits of *how* a learner learns (§4.8);
  each dimension has value + uncertainty + trait/state + source. Complements **Memory** (facts).
- **Learning event** — the immutable, KC-tagged interaction log (§4.5).
- **Content building block** — a reusable, KC-tagged, cacheable artifact: lesson, brief wiki,
  comprehensive wiki, question/flashcard. Assembled and re-framed per learner (§5.4 below).
- **Source & chunk** — ingested learner/curated material (documents, handwritten notes, audio/video,
  weblinks) normalized into embedded chunks carrying **provenance** for citations and compliance (§6).
- **Lesson plan** — the adaptive teaching policy for a learner + goal (§4.6).
- **Assessment item & rubric** — questions plus per-KC grading rubrics (§4.4).
- **Conversation / message** — tutor dialogue, persisted, streamed.
- **Memory** — persistent per-learner facts, preferences, and history that personalize every session.

### 5.4 Content strategy — hybrid by tier

Content is produced as **reusable concept-level building blocks** (AI-generated, KC-tagged, cached)
that are **assembled, sequenced, scaffolded, and re-framed** per learner. This keeps the *experience*
non-fungible while keeping *production* cost-controlled — essential at underserved scale. The mix is
**hybrid by tier**: heavy reuse for lower tiers and large cohorts; more bespoke generation for
advanced learners where nuance matters most.

---

## 6. Target Architecture

API-first backend (FastAPI) serving web first, then mobile/desktop. Layered and pragmatic — we add
abstraction only when it earns its keep.

```text
app/
  core/      config (pydantic-settings), async DB session/engine, logging, deps,
             stub-auth seam, beartype claw
  api/v1/    thin HTTP routers (versioned)
  schemas/   Pydantic request/response models
  models/    SQLAlchemy ORM
  services/  business logic / orchestration
  llm/       provider-agnostic LLM + MODEL-ROLE REGISTRY (FAST/SMART/GENIUS/EMBED →
             concrete model per environment). Providers: Ollama (dev), OpenRouter
             (prod), Bedrock/Vertex/Azure, Anthropic; FakeProvider (tests)
  prompts/   DSPy modules (offline-compiled) + the interactive prompt-refinement gate
  agent/     LangGraph orchestration: stateful graphs for tutoring turn, lesson
             generation, grading, content assembly; tool registry; HITL nodes
  rag/       multimodal ingestion (docs/OCR/ASR/web adapters → normalize → chunk →
             embed → store w/ provenance) + hybrid retrieval
  memory/    per-user long-term memory, conversation summarization, write-back
  learning/  EDUCATION CORE: knowledge graph, KnowledgeTracer (IRT/Elo), mastery
             aggregation, FSRS scheduler, lesson-plan policy, assessment + grading,
             content generation/assembly, analytics
db/migrations/   Alembic (first migration enables `vector`, `pg_trgm`)
frontend/        React + TypeScript + Vite (later phase)
docs/            MASTERPLAN.md, ROADMAP.md, TECHNICAL_DESIGN.md
```

> Full engineering detail for the LLM stack, orchestration, and ingestion lives in
> [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md). The summary below is the stable shape.

**Data layer (PostgreSQL):** `pgvector` (HNSW) for embeddings; `pg_trgm` + GIN and `tsvector`
full-text for keyword/fuzzy search; async SQLAlchemy 2.0 over `asyncpg`; Alembic migrations.
Retrieval is **hybrid** (vector + full-text + metadata filter).

**LLM strategy — roles, not models.** Code references **semantic roles**; config maps each role to
a concrete `(provider, model)` **per environment**, so models are swapped without touching logic:

| Role | Dev / test | Prod (example) | Used for |
| ---- | ---------- | -------------- | -------- |
| `FAST` | Ollama (Gemma/Qwen, local) | cheap OSS (gpt-oss / Gemma / Haiku) | KC tagging, routing, classification, the refinement gate |
| `SMART` | cheap cloud via OpenRouter | `claude-sonnet-4-6` | tutoring turns, grading, most generation |
| `GENIUS` | cheap cloud (sparingly) | `claude-opus-4-8` | hard reasoning, curriculum/graph synthesis |
| `EMBED` | local embedding model | cloud embeddings | vectorization for RAG |

Providers are environment-tiered: **Ollama** self-hosted for dev (FAST local; SMART/GENIUS routed to
a cheap cloud model since 100B-class models don't run usefully on a laptop), **OpenRouter** as the
first prod integration (broad model access, swappable), with **Bedrock/Vertex/Azure** as later
compliance/enterprise options. Knowledge sources: curated KB + user uploads + live/external (agentic
retrieval) + LLM general knowledge.

**Orchestration & prompt optimization.** **LangGraph** is the orchestration backbone — tutoring
turns, lesson generation, grading, and content assembly are stateful graphs (branching, retries,
human-in-the-loop). It sits *above* the role registry: a node requests a role, the registry returns
the env-appropriate client. Prompt quality is handled in two layers: **(a)** an **interactive
prompt-refinement gate** — a HITL subgraph that co-constructs the learner's goal/prompt in a loop
*until the learner is satisfied*, then commits to generation (also a pedagogical/metacognitive
moment); **(b)** **DSPy** compiles our internal modules (including the gate) offline against eval
metrics. Both are introduced when they earn their keep, behind thin seams (see TECHNICAL_DESIGN).

**Interaction modes:** one engine, three modes — conversational chat (supplementary), agentic
actions, and structured workflows — selected by need and tier.

**Streaming:** Server-Sent Events for token streaming.

**Background work:** FastAPI `BackgroundTasks` early; a lightweight real queue (**taskiq/arq +
Redis**) introduced when ingestion/content pre-generation lands. Used for multimodal ingestion,
content generation, memory write-back, and analytics aggregation.

**Multimodal ingestion.** A set of **source adapters** all normalize into one
`document → chunks → embeddings + provenance` pipeline: office docs (PDF/PPTX/DOCX/XLSX/TXT),
**handwritten notes via vision-LLM OCR**, **audio/video via ASR (Whisper)**, and **public weblinks**
(fetch + readability). Provenance metadata travels with every chunk for citations and the
privacy/compliance gate.

---

## 7. Key Technical Decisions & Rationale

| Decision | Choice | Why |
| -------- | ------ | --- |
| Mastery model | Continuous dynamic-IRT (Elo/Glicko) + uncertainty | Captures partial understanding; binary BKT loses signal. |
| Retention | FSRS, layered separately | Forgetting ≠ understanding; keep them orthogonal. |
| Mastery granularity | Per-KC, hierarchically rolled up | One subject score is lossy; learners need to drill in. |
| Learner profile | Evidence-based behavioral dimensions, **not VARK** | Modality-matching has no replicated effect; model what's observable + actionable. Modality = UX signal only. |
| Grading | LLM rubric grading from v1 | Enables partial credit; feeds the continuous tracer. |
| Future tracing | Swappable `KnowledgeTracer` + event log day one | Earns DKT training data for free; no rewrite later. |
| Curriculum | AI-generated for MVP | Fastest path; standards alignment is a later layer. |
| Content | Reusable blocks, personalized assembly | Non-fungible experience at controlled cost. |
| LLM | Provider-agnostic, **roles not models** | Swap models per env via config; flexibility + cost control without leaking into services. |
| Model tiers | `FAST` / `SMART` / `GENIUS` / `EMBED` | Right-size cost vs capability per task; concrete models are config. |
| Dev/test inference | Ollama (FAST local) + cheap cloud (SMART) + `FakeProvider` | Token-heavy tests stay cheap; deterministic unit tests stay offline. |
| Prod inference | OpenRouter first; hyperscalers later | One integration, broad models, swappable; defer compliance-grade hosting. |
| Orchestration | LangGraph stateful graphs | Branching/retries/HITL for multi-step LLM workflows. |
| Prompt quality | Interactive refinement gate + DSPy | Users lack optimal prompts; co-construct interactively, optimize modules offline. |
| Ingestion | Multimodal adapters → one pipeline | Docs/OCR/ASR/web unify into chunks+provenance for RAG. |
| OCR / ASR | Vision-LLM OCR + Whisper | Best on handwriting, least bespoke infra; fits the role registry. |
| Auth | Stubbed behind a seam | Defer identity; thread `learner_id` everywhere from day one. |
| Connectivity | Online-first | Ship the MVP; design data model so offline/sync can be added. |
| Monetization | None in MVP | Focus on the learning loop. |

---

## 8. Non-Functional Requirements

- **Scalability:** stateless API, async I/O throughout, real job queue for heavy/async work, content
  caching, hierarchical aggregation computed incrementally.
- **Cost (a first-class constraint):** per-LLM-call **token & cost logging from day one**; model
  routing; aggressive reuse/caching of generated content. The mission depends on per-learner cost
  staying low.
- **Privacy & compliance:** a **hard gate** — COPPA / FERPA / GDPR-K obligations must be met before
  any sub-adult tier. Thread data-minimization and deletion through the model now even though MVP is
  adult-only. **Behavioral/affective profiling (§4.8) is especially sensitive**: keep it transparent
  and learner-controllable (view/reset), and treat it as in-scope for that gate.
- **Observability:** structured logging, request IDs, LLM/tool call tracing, cost dashboards.
- **AI quality / evaluation:** an **eval harness** for educational correctness and grading
  reliability is part of the engineering loop, not an afterthought — wrong teaching is worse than no
  teaching.
- **Testability:** `FakeProvider` for deterministic LLM tests; transactional DB tests; golden/eval
  tests for educational artifacts.

---

## 9. Explicitly Deferred (parked, not forgotten)

Real authentication · billing/monetization · Guru-creator, teacher, and org-admin roles ·
teacher/parent dashboards · multi-tenant orgs & classes (B2B) · DKT · standards alignment
(Common Core/NGSS) · younger tiers + their child-privacy compliance · offline / low-bandwidth mode ·
mobile & desktop clients.

These shape the architecture (we leave seams) but are out of scope for the early phases in
[ROADMAP.md](./ROADMAP.md).
