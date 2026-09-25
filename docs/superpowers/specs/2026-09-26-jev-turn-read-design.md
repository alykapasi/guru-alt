# Jev turn read: a fast first pass in front of the LLM

**Tracker items:** **S78** (decision contract and isolated adapter), **S81** (shadow execution,
broadened from intent-only to the turn read), **S82** (decision-call accounting), **S83** (live
switch, redefined as per-question). The grading question comes forward from S85's "later" list.
S79 (offline comparison) and S80 (privacy before learner data) are re-scoped; see §9.

Branch `feat/jev-turn-read`, cut from `fix/auth-ui` (PR #40, unmerged) for its RustFS and CI
fixes. Becomes its own PR against `main` once #40 merges; nothing is pushed until asked.

---

## 1. What this decides

Jev (TypeSafe's System One model, `jev-1.13.0`) is not a language model. It reads a state and
answers typed questions about it — a label with probabilities, a yes/no probability, or a score
— several independent questions per request, fast (vendor claim 70–500 ms) and cheap ($0.042 per
million input tokens, output free). It writes no text, cannot be fine-tuned, and its judgments
are fallible semantic evidence; arithmetic, tracing, scheduling and policy stay in Guru code
(`docs/jev-capabilities.md`).

The earlier Jev plan used it as a like-for-like substitute for one cheap FAST call (the answer-
intent gate). This design uses it the way a System One model is meant to be used: **a fast first
pass that reads the learner's turn and decides which model calls are needed at all.** Savings come
from calls not made — above all the SMART grading call — and each decision earns its place by a
measured comparison with what today's model decided, not by vendor claims.

Decisions taken in brainstorming (2026-09-26):

| Question | Decision |
|---|---|
| Which role leads | The fast first pass on the practice/tutor turn. Per-turn learner signals (confusion, guessing, …) are the next slice, as more questions on the same read. |
| What traffic may go to Jev | All traffic, now. Nobody outside the founder uses Guru yet. The vendor privacy review (retention, deletion, agreement) is a **documented precondition for inviting anyone else**, not a code gate. |
| How a question goes live | Manually, per question, after the founder reads a shadow report. No automatic promotion. |
| Wiring | One read per learner turn where the code allows it (§3), not one Jev call per call site. |
| Retrieval need | Out of this slice. Nothing decides it today, so shadow mode would have no baseline to agree with. |
| Spending cap | None. At Jev's price a year of founder use is cents; the report shows spend. |

## 2. Components

### 2.1 `app/llm/decisions.py` — the decision client

- `DecisionClient` protocol with one method, `async read(state, questions, *, timeout, retries)
  -> DecisionResponse`.
- Guru-owned types:
  - `ChoiceQuestion(instructions, options: dict[str, str])`
  - `YesNoQuestion(instructions, yes: str | None, no: str | None)`
  - `ChoiceAnswer(label, probabilities, confidence)`
  - `YesNoAnswer(probability)` — Jev's Noul has no confidence field and none is invented.
  - `DecisionResponse(answers: dict[str, ChoiceAnswer | YesNoAnswer], model, input_tokens,
    latency_ms)`
  - `DecisionFailure(kind)` where `kind` is `timeout | rate_limited | auth | server | invalid`.
    `read` never raises: every SDK error, connection error, timeout, or an answer whose label is
    outside the options, comes back as a failure value.
- `TypeSafeDecisionClient` — **the only module that imports `typesafe_sdk`.** Wraps
  `AsyncTypeSafeClient.system_one`, passing a per-call `timeout` and `RetryPolicy` so the SDK's
  default (two retries inside a 30 s budget) never applies. Pins the `typesafe_sdk` logger at
  WARNING at construction: its DEBUG output includes request bodies.
- `FakeDecisionClient` — scripted answers, failures and latency for tests.
- The model id comes from settings (`decision_model`), never from code, mirroring the role
  registry.

### 2.2 `app/learning/turn_read.py` — the per-turn read

- Two named questions:
  - **`intent`** — Choice over `attempt | deferral | withdrawal`, with option descriptions taken
    from `TurnIntent`'s docstrings and the same instruction as the FAST prompt, including "when
    the reply could be read either way, prefer deferral over attempt".
  - **`fully_correct`** — Yes/No: does the learner's answer fully meet the question and its rubric
    criteria?
- State: the question stem, the rubric criteria (for `fully_correct`), and the learner's message
  fenced with `as_untrusted`, as the grader already does (S31).
- `start_read(client, *, questions, stem, message, rubric) -> TurnRead` starts the request
  immediately (an `asyncio.Task`) and returns a handle. A question whose mode is `off` is never
  included; if every requested question is `off`, no request is made.
- `TurnRead.answer(name, *, deadline)` waits at most `deadline` for the task and returns the
  answer or a failure.
- `TurnRead.record(name, *, baseline, used, …)` writes that question's `decision_calls` row
  (§2.4). In shadow mode this is scheduled as a background task holding its own reference, so a
  turn that finishes first neither waits for it nor lets it be garbage-collected.

### 2.3 Settings (`app/core/config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `typesafe_api_key: SecretStr` (`GURU_TYPESAFE_API_KEY`) | empty | Already in the founder's `.env`. |
| `decision_model` | `jev-1.13.0` | |
| `decision_intent_mode` | `off` | `off \| shadow \| live` |
| `decision_fully_correct_mode` | `off` | `off \| shadow \| live` |
| `decision_intent_threshold` | `0.9` | Choice confidence at or above which a live intent is used. |
| `decision_fully_correct_threshold` | `0.9` | P(yes) at or above which a live answer is passed. |
| `decision_live_deadline_ms` | `800` | Past this, a live question falls back to today's path. |
| `decision_shadow_timeout_s` | `5.0` | Shadow requests; nobody waits on them. |

Settings validation refuses to start the app when any mode is `shadow` or `live` and the key is
empty, naming the setting. With every mode `off` no key is needed — CI and the e2e journeys keep
the defaults.

### 2.4 `decision_calls` table

Written on its own transaction, like `llm_calls` (`app/services/llm_log.py`), so a turn that
rolls back still leaves the request recorded.

| Column | Notes |
|---|---|
| `id`, `created_at` | |
| `learner_id`, `conversation_id`, `item_id`, `attempt_id` | nullable, `ON DELETE SET NULL`; `attempt_id` set where grading recorded one |
| `question` | `intent \| fully_correct` |
| `mode` | `shadow \| live` (`off` writes nothing) |
| `model` | |
| `status` | `ok \| timeout \| rate_limited \| auth \| server \| invalid` |
| `answer` | the label, or `null` for yes/no |
| `probabilities` | JSONB: the label distribution, or `{"yes": p}` |
| `confidence` | Choice only |
| `baseline_intent` | what the FAST gate decided (intent rows) |
| `baseline_score` | the SMART score (fully_correct rows) |
| `used` | true when Jev's answer decided the outcome (live, above threshold) |
| `input_tokens`, `cost_usd`, `latency_ms` | cost from `pricing.py` |

**No learner text is stored.** The report reaches examples through `attempt_id` (the attempt
keeps the response) and, for intent rows, through the conversation's learner message.

`pricing.py` gains Jev: $0.042 per million input tokens, $0 output.

### 2.5 The report — `uv run poe decision-report`

A script over `decision_calls` and the joined attempts/messages, optionally limited by `--since`.
It prints per question:

- **intent:**
  - agreement with FAST, overall and by confidence band
  - a 3×3 confusion matrix
  - the share at or above threshold
  - FAST calls and dollars that would have been saved, priced from the mean FAST cost in
    `llm_calls`
  - the two harmful directions called out separately:
    - Jev *attempt* where FAST said deferral or withdrawal: a non-answer would be graded.
    - Jev *withdrawal* where FAST said attempt: evidence would be dropped.
- **fully_correct:**
  - of answers at or above threshold, how many SMART scored below 1.0 and the mean shortfall
  - **how many SMART failed outright (below `PASS_THRESHOLD`, 0.6) — false passes**, the number
    that decides whether the question may go live
  - the SMART calls and dollars that would have been saved
- **both:**
  - status counts (timeouts, errors)
  - latency p50, p95 and p99
  - Jev spend
  - up to N disagreement examples

## 3. How a turn flows

Answers reach grading by three routes. The workflow graph is checkpointed and its state must stay
serializable, so a live request cannot travel into it; that decides where the read starts.

| Route | Intent gate | Grading | Read |
|---|---|---|---|
| Conversational check (`services/chat.py`, open check) | `classify_intent` | `answer_item` in the same function | **one** request, `intent` + `fully_correct`; the handle is passed to `answer_item` |
| Guided practice (`_choose_flow` → `practice.classify_paused_message` / workflow resume) | yes | inside the graph | `intent` at the gate; `_grade` starts its own `fully_correct`-only request |
| Direct submit (`POST …/answer`) | none | `answer_item` | `_grade` starts a `fully_correct`-only request |

`answer_item` and `_grade` take an optional `read: TurnRead | None`. When none is given and
`fully_correct` is not `off`, `_grade` starts one — only for rubric-graded items, since objective
and flashcard items make no model call to save.

### 3.1 Behaviour per mode

| Mode | Intent gate | Grader (rubric items) |
|---|---|---|
| `off` | FAST, as today; no Jev request | SMART, as today |
| `shadow` | FAST decides. After it returns, the row is recorded with `baseline_intent`; a Jev answer not yet back gets the rest of the shadow timeout, then records `timeout`. | SMART grades as today; the row records Jev's P(yes) beside `baseline_score`. |
| `live` | Jev's label is used when `status = ok` and confidence ≥ threshold, and the FAST call is skipped (`used = true`). Otherwise FAST runs as today. | When P(yes) ≥ threshold: `GradeResult(score=1.0, correct=True, detail={"method": "decision"})` with no rationale, no diagnosis and no component scores (each KC falls back to the aggregate), and the SMART call is skipped. Otherwise SMART as today. |

**Jev never fails an answer.** A wrong answer needs SMART's rationale and misconception diagnosis
(S09), which Jev cannot produce, so only a confident *pass* skips the SMART call.

In live mode, a question that falls back still gets its row, with `used = false`, and a baseline
where today's path ran.

## 4. Failures and guardrails

- **A Jev failure never breaks or changes a turn.** Every failure is a `DecisionFailure`, and
  every failure means today's path runs.
- **Deadlines instead of retries.** Live: zero retries, timeout = `decision_live_deadline_ms`.
  Shadow: zero retries, timeout = `decision_shadow_timeout_s`.
- **Shadow is isolated.** Shadow work never writes to the turn's session and never delays the
  response. A property test per consumer asserts that shadow output cannot change the outcome,
  even when Jev disagrees, times out or errors.
- **Nothing sensitive in logs.** Structured log lines carry the question, status, answer,
  confidence and latency, never the state sent. The SDK logger stays at WARNING.
- **Privacy precondition, documented.** Before any learner other than the founder is invited, the
  open questions in `docs/jev-capabilities.md` ("Data handling and unresolved questions") must be resolved: agreement, retention,
  deletion and ZDR eligibility. This is recorded in the RUNBOOK and the tracker, not enforced in
  code.

## 5. Testing

All unit tests run against `FakeDecisionClient` with no network.

- **Decision client:**
  - mapping Guru question types to the SDK's and results back
  - each SDK error class and timeout becoming the matching `DecisionFailure`
  - an out-of-set label becoming `invalid`

  These run against a stubbed HTTP transport, which the SDK accepts, so the SDK-importing module
  is covered without a key.
- **Turn read:**
  - the questions included per route and mode
  - no request when all are off
  - the learner text fenced
  - the `intent` instruction kept in step with the FAST `_SYSTEM_PROMPT`: shared wording pinned by
    a test, as `test_shaped_provider` pins prompt markers
- **Modes, per consumer:**
  - `off` makes no request.
  - `shadow` leaves the outcome identical to `off` under agreement, disagreement, timeout and
    error.
  - `live` uses Jev above the threshold and falls back below it, on timeout and on error.
  - `live` grading never yields a failing grade from Jev, and a Jev pass carries
    `method: "decision"` and no diagnosis.
- **Startup:** validation rejects a mode that is on with an empty key.
- **Accounting:**
  - rows are written with baselines
  - a shadow row survives the turn's transaction rolling back
  - cost comes from `pricing.py`
- **Report:** computed over hand-built rows, covering:
  - agreement bands
  - the confusion matrix
  - the harmful directions
  - false passes below 0.6
  - savings
- **Integration:** one test per route (conversational check, guided practice, direct submit) with
  the fake in shadow mode. It asserts the learner-visible result equals the `off` result and the
  expected rows exist.
- **Live smoke test:** `GURU_JEV_SMOKE=1` sends a few synthetic turns to the real service and
  checks the answer shapes and reports latency. It is skipped by default and in CI.
- **Unchanged:** CI and the e2e journeys run with every mode `off`.

## 6. Out of scope

- Retrieval need (no baseline).
- Per-turn learner signals (next slice).
- Direct substitution of the link judge, citation support and KC tagging.
- An admin page for the report.
- Automatic promotion.
- Using Jev's Score primitive for partial credit.

## 7. Docs

- **`docs/jev-architecture.md`, `docs/jev-implementation-plan.md`:** reframe Jev as the fast first
  pass, the turn read, all-traffic shadow, per-question manual promotion, and the privacy review
  as a precondition for outside learners. The phases superseded by this design are marked as such
  rather than deleted.
- **`docs/RUNBOOK.md` §14 "Jev turn read":**
  - turning a question to shadow
  - reading the report
  - the criteria for flipping to live (false passes first)
  - rolling back (set the mode to `off`)
  - the privacy precondition
- **`.env.example`:** the new settings, all off.
- **`CLAUDE.md`:** one Key Technical Decisions bullet: Jev is a first pass whose confident answers
  may skip a model call, never a replacement for a model's text, and never the author of a failing
  grade.

## 8. Rollout

1. Merge with everything `off`.
2. Set both questions to `shadow` locally and use Guru normally.
3. Run the report. For each question, the founder decides whether to flip it to `live` and at what
   threshold, and can set it back to `off` at any time.

## 9. Tracker changes

| Row | Change |
|---|---|
| S78 | Implemented as specified (§2.1, §2.3). |
| S79 | Superseded: the shadow report on real founder traffic replaces the offline labelled comparison. A hand-labelled set remains a possible later check. |
| S80 | Re-scoped: resolving it is a precondition for inviting other learners, not for shadow traffic. Stays open. |
| S81 | Broadened to the turn read: `intent` and `fully_correct`. |
| S82 | `decision_calls`, pricing, the report. |
| S83 | Redefined: a manual per-question `live` switch with threshold and fallback, replacing the invited-cohort experiment. |
| S85 | The grading second opinion moves into this slice as `fully_correct`. The rest stays later. |

Commit subjects carry one of S78, S81, S82 or S83.
