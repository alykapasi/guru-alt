# New Subject Onboarding: Closing Phase 7's Subject-Creation Gap

## Overview

**Problem:** Phase 7 left learners with no way to create a Subject/Topic/KC graph from the UI. Phase 1 built the knowledge graph as raw CRUD endpoints; no subsequent phase wired a frontend flow to create one. This means `/app/lessons` dead-ends at "No subjects yet" and the only reachable screen is a subject-less general chat that reads as a vanilla chatbot — none of the adaptive tutor's infrastructure (lesson plan, quizzes, grounding, mastery model) can activate without a subject.

**Solution:** A four-step wizard (`/app/subjects/new`) that turns a rough idea into a committed Subject/Topic/KC graph:
1. Select materials (optional, reuses existing upload UI)
2. Refine goal via HITL back-and-forth (reuses existing refinement gate)
3. Review/edit LLM-proposed curriculum breakdown
4. Commit to the database

Once committed, the learner lands on `/app/lessons` for the new subject where the existing placement + lesson-plan flows take over unchanged, pre-filled with the agreed goal.

**Success Criteria:**
- A fresh learner can reach a lesson plan and take their first quiz within the UI (no scripts/API calls needed)
- Backward compatible — existing /app/chat and /app/lessons flows unchanged
- Reuses existing backends (refinement gate, lesson plan generation, item generation) — no new orchestration patterns

---

## User Flow

### Entry Points
1. **Landing page** — "Get started" button routes to `/app/subjects/new` for a truly first-time learner
2. **Lessons page empty state** — "No subjects yet" text replaced with "Create your first subject" button routing to `/app/subjects/new`
3. **After completing a subject** — future "start another subject" CTA (deferred, mentioned for flow completeness)

### The Four Steps

#### Step 1: Materials (Optional)
- **UX:** Checklist of learner's existing unscoped sources (if any) + an inline "Upload new" button
- **Behavior:**
  - Empty state: shows only "Upload new" — learner can proceed without selecting anything
  - Non-empty: learner ticks checkboxes to select 0+ sources to ground curriculum generation
  - Inline upload reuses the existing `UploadForm` component (subject-id-less on first pass; once Subject is created, future materials go to that subject by default)
- **Acceptance:** "Next" proceeds regardless; selection is optional

#### Step 2: Goal Refinement
- **UX:** Streaming conversational interface (identical visual treatment to the existing chat refinement gate)
- **Backend:** Reuses `build_refinement_graph` + `refinement_config` as-is — no Conversation created, no message history persisted. Orchestration lives in a new `OnboardingService` that mirrors `run_refinement_turn`'s shape but discards the transcript once `agreed_goal` is committed
- **Behavior:**
  - LLM proposes a refined, scoped goal based on learner's initial input
  - Learner can accept ("Looks good") or request changes ("More focus on X", "Too broad")
  - Loop exits on max rounds (5, matching refinement-gate default) or learner acceptance — only the final `agreed_goal` string is preserved for the next step
- **Acceptance:** "Looks good" commits the goal and proceeds to step 3

#### Step 3: Review Curriculum
- **UX:** Editable nested list (Topic rows, each expandable to show its KCs)
  - Per-topic: name input, description input, remove button
  - Per-KC: name input, description input, remove button
  - No drag-to-reorder, no "add new topic" button in v1 (recovery path is "regenerate," not manual authoring from scratch)
- **Backend:** `POST /onboarding/curriculum` (one-shot, not resumable) takes `{goal, source_ids}` and returns a structured curriculum proposal `{subject_name, subject_description, topics: [{name, description, kcs: [{name, description}]}]}`
- **Behavior:**
  - Learner reviews the LLM's proposed breakdown
  - Inline edits are allowed (rename topics/KCs, remove them) — no add-new-from-scratch
  - If the proposal is unacceptable, a "Try again" button re-calls the LLM (same model call, different seed)
  - Acceptance proceeds to step 4

#### Step 4: Commit & Redirect
- **UX:** Final "Create subject" button + confirmation message
- **Backend:** `POST /subjects/commit` in a single transaction:
  - Create `Subject` row (auto-slug from name, de-duplicate on collision via suffix)
  - Create `Topic` rows under it
  - Create `KC` rows under each topic
  - Reassign selected source rows: set their `subject_id` to the new subject
  - Return the created `SubjectRead` + subject ID for redirect
- **Behavior:**
  - On success: redirect to `/app/lessons?subject_id=<new_id>` where `NoPlanCard` greets the learner with placement + "Generate lesson plan" CTAs, pre-filled with the `agreed_goal`
  - On error: show error message, "Try again" button re-attempts the commit (idempotent by subject name, or returns conflict if name already taken)

---

## Architecture

### Backend

#### New File: `app/learning/curriculum.py`
Pure policy layer for curriculum generation. Mirrors `app/learning/item_generation.py`'s pattern.

**Function:** `async def generate_curriculum(llm, goal, materials: list[str] | None) -> CurriculumProposal | None`
- **Input:**
  - `goal`: refined goal string from the onboarding gate
  - `materials`: optional list of representative chunk excerpts (50–200 words each) from selected sources, or `None` if no sources selected
- **Output:** Structured proposal `{subject_name, subject_description, topics: [{name, description, kcs: [{name, description}]}]}` or `None` on parse failure
- **Behavior:**
  - SMART role (higher stakes than MCQ generation; structural output requires the best model available per the role registry)
  - System prompt guides the LLM to propose 3–5 topics, 2–4 KCs per topic (v1-arbitrary heuristics, not calibrated)
  - JSON-only reply (tolerant parsing, returns `None` on malformed JSON, not a fatal error)
  - If `materials` is `None`, prompt says "no reference materials provided; use your knowledge" — curriculum is purely knowledge-based, not grounded
  - If `materials` provided, includes them inline: "ground your breakdown in these excerpts: [excerpt 1] [excerpt 2] ..."

**Error handling:** Malformed/empty reply returns `None` — caller (frontend) shows a "Try again" action that re-calls this function.

#### New File: `app/services/onboarding.py`
Persistence orchestration for the onboarding flow. Mirrors `app/services/refinement.py`'s shape.

**Functions:**
1. `async def run_goal_refinement_turn(llm, session_id, user_content, satisfied, resume) -> AsyncIterator[TurnEvent]`
   - Thin wrapper over `build_refinement_graph` (reused as-is, accepts arbitrary `thread_id`)
   - No Conversation created; in-memory checkpointer holds state for the negotiation's lifetime
   - Returns same `TurnEvent` union as `run_refinement_turn` (token, awaiting_reply, committed, error)
   - On "committed", the final `snapshot.values["agreed_goal"]` is what the frontend captures for the next step

2. `async def generate_curriculum(session, llm, goal, source_ids) -> CurriculumProposal | None`
   - Fetches chunk excerpts from selected sources (if any) via `app.rag.retrieval`
   - Calls `curriculum.generate_curriculum(llm, goal, materials)`
   - No logging of the LLM call (it's ephemeral, not part of the learner's audit trail until materials are actually used)

#### Modified Files: `app/services/knowledge.py`

**New function:** `async def create_subject_with_graph(session, subject_data) -> Subject`
- Atomically creates a Subject, Topics, KCs, and reassigns source references in one transaction
- Input: `{subject_name, subject_description, topics: [{name, description, kcs: [{name, description}]}], source_ids}`
- Auto-slug generation: `subject_slug = slugify(subject_name)`; if collision, append `_2`, `_3`, etc.
- Returns created `Subject` + eager-loaded topics/kcs for response
- Called by the new `POST /subjects/commit` endpoint

#### New Endpoints: `app/api/v1/onboarding.py`

**`POST /onboarding/goal-turns`** (SSE, like `/conversations/{id}/messages`)
- Request: `{session_id, content, satisfied, mode}` where `mode` is "start" or "resume"
- Streams `TurnEvent` (token, awaiting_reply, committed, error)
- `session_id` is minted by the frontend (UUID) — no DB coupling, purely for in-memory checkpointer keying
- Mirrors `/conversations/{id}/messages`'s streaming shape end-to-end

**`POST /onboarding/curriculum`** (one-shot JSON)
- Request: `{goal, source_ids: list[UUID] | null}`
- Response: `{subject_name, subject_description, topics: [{...}]}`  or 400 error on LLM failure
- Calls `onboarding.generate_curriculum(session, llm, goal, source_ids)`

**`POST /subjects/commit`** (new endpoint in `app/api/v1/knowledge.py`)
- Request: `{subject_name, subject_description, topics: [{name, description, kcs: [{name, description}]}], source_ids: list[UUID] | null}`
- Response: `SubjectRead` (includes created id)
- Calls `knowledge.create_subject_with_graph(session, ...)`
- Returns 409 if subject name already exists (learner must pick a different name or get a count)
- Calls `enqueue(source.id)` on each reassigned source to trigger re-indexing with the new subject scope (if not already done)

#### Modified Files: `app/services/ingestion.py`, `app/api/v1/sources.py`
- **Verify** (no code change needed): `Source.subject_id` is already `uuid.UUID | None` (nullable), so sources can be created unscoped and reassigned later
- Confirm that `POST /sources/upload` accepts `subject_id=None` (should already work; Phase 7's Uploads page can already upload unscoped materials)

### Frontend

#### New Route: `/app/subjects/new`

**Component:** `SubjectWizard` (full-page)
- Local state: `{step, materials, agreeedGoal, curriculumProposal, isLoading, error}`
- Four child components:
  - `MaterialsStep` — checkbox list + inline upload button
  - `GoalStep` — SSE consumer, reuses streaming message UI from `useChatConversation`
  - `ReviewStep` — editable nested list
  - `CommitStep` — final confirm (or inline during step 4)
- Navigation: "Next" / "Previous" buttons per step, "Create subject" on step 4

#### New Hooks: `frontend/src/api/hooks.ts`

```typescript
export function useGoalRefinement(sessionId: string | undefined) {
  // SSE consumer, points at POST /onboarding/goal-turns
  // Returns { proposal, isDone, error, isLoading, send }
  // Mirrors useChatConversation's pattern
}

export function useGenerateCurriculum() {
  // useMutation, POST /onboarding/curriculum
  // mutationFn: async ({ goal, sourceIds }) => {...}
}

export function useCommitSubject() {
  // useMutation, POST /subjects/commit
  // mutationFn: async (data) => {...}
  // onSuccess: invalidate ["subjects"]
}
```

#### Modified Components

**`frontend/src/pages/Lessons.tsx`** (minor)
- `NoPlanCard`'s empty-state text replaced with a button: "Create your first subject" → `/app/subjects/new`

**`frontend/src/pages/Landing.tsx`** (minor)
- "Get started" button points to `/app/subjects/new` instead of `/app/chat`

#### Reused UI Patterns
- `UploadForm` (existing) — inlined in MaterialsStep for new-source upload
- `MessageList` + `Composer` streaming UI (existing) — styled for GoalStep's refinement UI
- "Try again" error recovery — matches existing chat error handling

---

## Error Handling & Edge Cases

### LLM Failures
- **Malformed curriculum JSON:** `POST /onboarding/curriculum` returns 400 with a human-readable message; frontend shows "Try again" button re-calling the mutation
- **Refinement max rounds:** Same as the existing gate — auto-commits the last proposal if max_rounds reached (REFINEMENT_MAX_ROUNDS = 5)
- **Refinement generation failure:** SSE error frame, frontend shows "Something went wrong" + "Try again" (resume from the same session_id)

### Name Collisions
- Subject name already exists → `POST /subjects/commit` returns 409; frontend prompts learner to pick a different name (no auto-suffix from client)
- KC name collision within the subject → handled by DB uniqueness constraint; frontend never allows submitting a duplicate (validation on the client side, or server rejects with 422 and frontend shows "KC names must be unique within a subject")

### No Materials Selected
- Curriculum generation proceeds with materials = `None` (fully knowledge-based, no grounding)
- Sources can still be uploaded + reassigned after the subject is created (via `/app/uploads`)

### Mid-flow Navigation Away
- Wizard state is lost (intentional — no persistence until step 4)
- Learner can restart at `/app/subjects/new` and redo the flow
- No "save draft" or "continue later" feature in v1

---

## Testing

### Backend

**`tests/test_curriculum.py`** (unit tests for `curriculum.py`)
- ✓ Parses a well-formed curriculum reply
- ✓ Returns None on malformed JSON
- ✓ Returns None on empty topics array
- ✓ Returns None on topic/KC with missing required fields
- ✓ Includes materials excerpts in the prompt when provided
- ✓ Omits materials section when materials=None

**`tests/test_onboarding.py`** (integration tests)
- ✓ `run_goal_refinement_turn` streams tokens and awaiting_reply event
- ✓ Resumes from same session_id without re-running the propose node
- ✓ Max rounds auto-commits
- ✓ `generate_curriculum` fetches excerpts from selected sources
- ✓ `create_subject_with_graph` creates Subject + Topics + KCs in one transaction
- ✓ `create_subject_with_graph` reassigns Source rows' subject_id
- ✓ `create_subject_with_graph` de-duplicates subject slug on collision

**`tests/test_knowledge.py`** (existing, add to)
- ✓ `POST /subjects/commit` 201 + SubjectRead
- ✓ `POST /subjects/commit` 409 on name collision
- ✓ `POST /subjects/commit` triggers source re-indexing via enqueue()

### Frontend

**Live verification** (no new test runner this phase, matching Phase 7's precedent)
- Load `/app/subjects/new` as a fresh learner
- Select materials (or skip)
- Go through goal refinement (accept first proposal)
- Edit the curriculum (rename a topic, remove a KC)
- Commit and verify redirect to `/app/lessons?subject_id=<id>`
- Verify the new subject appears in the subject picker
- Verify "Generate lesson plan" is available with the goal pre-filled
- Verify a lesson plan can be generated and a quiz can be started

---

## Deferred / Future Work

- **LLM-authored prerequisite edges** (`KCEdge` rows) — v1 ships a flat ordered list; `generate_lesson_plan` already handles no-edges gracefully, falling back to declaration order. Real prerequisite detection is a future refinement, not a blocker.
- **Add-new-topic-from-scratch in ReviewStep** — v1 only allows editing what the LLM proposed; manual authoring in the review step deferred.
- **Subject-level material upload** — v1 requires uploading to unscoped materials first, then assigning to a subject during onboarding. A v2 shortcut is "upload directly to this new subject" without the reassign step.
- **Multi-subject prerequisite edges** — KCEdge today is intra-subject; cross-subject prerequisites (e.g. "Calculus requires Algebra") are deferred.

---

## Success Criteria (DoD)

✓ A fresh learner can create a Subject/Topic/KC graph from `/app/subjects/new` via the UI
✓ The new subject becomes available in `/app/lessons` for lesson-plan generation and guided practice
✓ Existing chat (`/app/chat`), lessons (`/app/lessons`), and dashboard flows work unchanged
✓ `uv run poe check` passes (lint, type-check, tests)
✓ Live verification confirms the full onboarding → lesson-plan → quiz → dashboard flow works end-to-end
✓ Design doc and implementation plan are reviewed before coding begins

---

## Open Questions for Implementation

- Should the Materials step be a dedicated page or a collapsible section above the Goal step? (Recommendation: dedicated step, clearer progression)
- Should ReviewStep allow bulk removal of all KCs from a topic, leaving it empty? (Recommendation: yes, but warn; empty topics are harmless)
- Should `curriculum.generate_curriculum` run with adaptive thinking enabled (higher reasoning cost/latency)? (Recommendation: no, SMART tier is good enough for v1; revisit if outputs are low quality)

---
