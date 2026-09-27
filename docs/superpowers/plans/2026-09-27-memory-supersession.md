# Memory Supersession Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Memory write-back decides "same / updates / coexists" with an explicit FAST judgement instead of one distance threshold; a replacement is visible and undoable; forgetting a conversation suppresses re-extraction only from that conversation.

**Architecture:** A small judge module (`app/memory/supersession.py`) takes candidates with their near neighbours and returns verdicts from one batched FAST call, defaulting to `coexists` on any doubt. `write_back` settles what it can for free (suppression, identical duplicates, no neighbours) and sends the rest to the judge. A `forgotten_scope` column scopes suppression. An undo endpoint and `MemoryRead.replaced` make replacements visible and reversible.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, pgvector, Alembic, pytest; React + TypeScript, TanStack Query, vitest.

**Spec:** `docs/superpowers/specs/2026-09-27-memory-supersession-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract` (after `uv run poe api-types`, stage `frontend/src/api/schema.d.ts`).
- Frontend changes also green on `cd frontend && npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, and `npm run lint` (ESLint + Prettier on the whole tree).
- New migration → `uv run python -m tests.testdb`. Alembic revision ids ≤ 32 characters.
- LLM access only through `app/llm` by role (`ModelRole.FAST` for the judge); never a model name. Log every call with `log_llm_call`.
- Text that came from a conversation (existing memories) goes into a prompt only fenced with `app.agent.untrusted.as_untrusted`.
- Doubt never deletes: any judge failure or malformed verdict means `coexists`.
- Async tests: re-read with `populate_existing=True`; read ids into locals before code that commits.
- One tracker id per commit subject: `[S42]`.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls (tests use `fake_llm_client`, scripting replies with `FakeTurn`).

## Review Focus

1. The judge answers `updates` naming a neighbour that a *previous* item in the same write-back already superseded — the second supersede must not orphan a chain or supersede a non-current row (Task 2 test).
2. Undo when the replaced row was since forgotten or corrected — 409, nothing changes (Task 4 test).
3. A conversation-scoped tombstone and a learner-scoped tombstone for the same fact — the learner-scoped one still suppresses everywhere (Task 3 test).
4. The judge's reply references a candidate or neighbour index out of range, or duplicates a candidate — ignored, that candidate coexists (Task 1 test).
5. Undo, then the same write-back content arrives again from a new conversation — it is judged afresh, not suppressed (Task 4 test).

---

### Task 1: The supersession judge [S42]

**Files:**
- Create: `app/memory/supersession.py`, `tests/test_memory_supersession.py`
- Modify: `app/core/config.py` (`memory_related_max_distance: float = 0.25`, beside `memory_dedup_max_distance`)

**Interfaces:**
- Produces:
  ```python
  JUDGE_ROLE = ModelRole.FAST
  Verdict = Literal["same", "updates", "coexists"]
  @dataclass(frozen=True) class Candidate: content: str; neighbours: Sequence[str]
  @dataclass(frozen=True) class Judgement: verdict: Verdict; target: int | None = None  # 0-based index into neighbours, only for "updates"
  COEXISTS = Judgement("coexists")
  async def judge(llm: LLMClient, candidates: Sequence[Candidate]) -> tuple[list[Judgement], Usage]
  ```
  `judge` returns exactly `len(candidates)` judgements, in order. No candidates → `([], Usage())` with no call. Any exception from the model call, and any unusable entry, yields `COEXISTS`.

- [ ] **Step 1: Failing tests** — `tests/test_memory_supersession.py`:

```python
"""Deciding whether a new memory is the same as, updates, or sits beside an existing one (S42)."""

import json

from app.agent.untrusted import INSTRUCTION
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.memory import supersession as sup

CANDIDATES = [
    sup.Candidate("Studies in the evenings now.", ["Studies in the mornings."]),
    sup.Candidate("Likes chess.", ["Likes football.", "Plays the piano."]),
    sup.Candidate("Is revising for the physics exam.", ["Is revising for the physics exam!"]),
]


def _reply(verdicts: list[dict]) -> str:
    return json.dumps({"verdicts": verdicts})


async def test_each_verdict_is_read() -> None:
    llm = fake_llm_client(
        _reply(
            [
                {"candidate": 1, "verdict": "updates", "replaces": 1},
                {"candidate": 2, "verdict": "coexists"},
                {"candidate": 3, "verdict": "same"},
            ]
        )
    )
    got, _usage = await sup.judge(llm, CANDIDATES)
    assert got == [sup.Judgement("updates", 0), sup.COEXISTS, sup.Judgement("same")]


async def test_anything_unusable_coexists() -> None:
    llm = fake_llm_client(
        _reply(
            [
                {"candidate": 1, "verdict": "updates", "replaces": 5},  # no such neighbour
                {"candidate": 2, "verdict": "merges"},  # no such verdict
                {"candidate": 9, "verdict": "same"},  # no such candidate
                {"candidate": 1, "verdict": "same"},  # a second answer for candidate 1: ignored
            ]
        )
    )
    got, _ = await sup.judge(llm, CANDIDATES)
    assert got == [sup.COEXISTS, sup.COEXISTS, sup.COEXISTS]


async def test_a_bad_reply_or_a_failed_call_coexists() -> None:
    got, _ = await sup.judge(fake_llm_client("not json"), CANDIDATES)
    assert got == [sup.COEXISTS] * 3

    class Broken:
        async def complete(self, *_a, **_k):
            raise RuntimeError("provider down")

        def spec(self, _role):
            return None

    got, _ = await sup.judge(Broken(), CANDIDATES)  # type: ignore[arg-type]
    assert got == [sup.COEXISTS] * 3


async def test_nothing_to_judge_makes_no_call() -> None:
    llm = fake_llm_client(script=[FakeTurn(text="should not be read")])
    got, usage = await sup.judge(llm, [])
    assert got == [] and usage.total_tokens == 0


async def test_existing_memories_are_fenced_as_untrusted() -> None:
    llm = fake_llm_client(_reply([]))
    await sup.judge(llm, CANDIDATES)
    provider = llm._providers["fake"]  # the FakeProvider records what it was sent
    system, messages = provider.prompts_sent[0]
    prompt = str(messages[0].content)
    assert "Studies in the mornings." in prompt
    assert INSTRUCTION in (system or "") or INSTRUCTION in prompt
```

(If `LLMClient` stores providers under another attribute name, read `app/llm/registry.py` and use it; if `as_untrusted`'s fence marker is exposed differently than `INSTRUCTION`, assert on what `as_untrusted` actually emits — read `app/agent/untrusted.py`.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_memory_supersession.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `app/memory/supersession.py`:

```python
"""Is a new memory the same as, an update to, or something beside an existing one? (S42)

Distance alone cannot tell "studies in the evenings now" (replaces "studies in the mornings")
from "is revising for chemistry" (sits beside "is revising for physics"). Write-back sends the
candidates that have near neighbours here, in one FAST call per write-back, and applies what
comes back.

Doubt never deletes. A failed call, an unparseable reply, a missing verdict, or one naming a
neighbour that does not exist all mean ``coexists``: the worst case is two memories where one
should have replaced the other, which the learner can see and fix, rather than a true memory
silently retired.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import structlog

from app.agent.untrusted import as_untrusted
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage

log = structlog.get_logger(__name__)

JUDGE_ROLE = ModelRole.FAST

Verdict = Literal["same", "updates", "coexists"]
_VERDICTS: frozenset[str] = frozenset({"same", "updates", "coexists"})


@dataclass(frozen=True)
class Candidate:
    """A newly extracted memory and the existing memories close enough to be about it."""

    content: str
    neighbours: Sequence[str]


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    # 0-based index into the candidate's neighbours; set only for "updates".
    target: int | None = None


COEXISTS = Judgement("coexists")

_SYSTEM_PROMPT = (
    "You maintain what a tutor remembers about one learner. For each numbered NEW statement you "
    "get the learner's existing memories that look related. Decide, for each NEW statement:\n"
    '- "same": it says the same thing as one of them (a rewording); nothing new.\n'
    '- "updates": it replaces one of them — the learner changed or corrected it, so the old '
    "one is no longer true. Give that memory's number as \"replaces\".\n"
    '- "coexists": both can be true at once; keep both.\n'
    "When unsure, answer coexists. Respond with ONLY a JSON object "
    '{"verdicts": [{"candidate": <n>, "verdict": "same"|"updates"|"coexists", '
    '"replaces": <m, only for updates>}]} and nothing else.'
)


async def judge(
    llm: LLMClient, candidates: Sequence[Candidate]
) -> tuple[list[Judgement], Usage]:
    """One verdict per candidate, in order. No candidates → no call."""
    if not candidates:
        return [], Usage()
    try:
        completion = await llm.complete(
            JUDGE_ROLE,
            [ChatMessage(role=ChatRole.USER, content=_build_prompt(candidates))],
            system=_SYSTEM_PROMPT,
            max_tokens=64 + 48 * len(candidates),
        )
    except Exception:
        log.warning("memory.supersession_judge_failed", exc_info=True)
        return [COEXISTS] * len(candidates), Usage()
    return _parse(completion.content, candidates), completion.usage


def _build_prompt(candidates: Sequence[Candidate]) -> str:
    blocks = []
    for n, candidate in enumerate(candidates, start=1):
        existing = "\n".join(f"{m}. {text}" for m, text in enumerate(candidate.neighbours, start=1))
        blocks.append(
            f"NEW {n}: {candidate.content}\n"
            f"{as_untrusted(f'EXISTING MEMORIES FOR NEW {n}', existing)}"
        )
    return "\n\n".join(blocks)


def _parse(content: str, candidates: Sequence[Candidate]) -> list[Judgement]:
    judgements: list[Judgement | None] = [None] * len(candidates)
    # A candidate's first entry is its answer, valid or not: a later entry for the same
    # candidate is ignored rather than allowed to overrule it.
    seen: set[int] = set()
    try:
        start, end = content.find("{"), content.rfind("}")
        raw = json.loads(content[start : end + 1])["verdicts"] if start != -1 else []
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raw = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            index = int(entry["candidate"]) - 1
            verdict = str(entry["verdict"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= index < len(candidates) or index in seen:
            continue  # out of range, or a second answer for the same candidate
        seen.add(index)
        if verdict not in _VERDICTS:
            continue
        if verdict == "updates":
            try:
                target = int(entry["replaces"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if not 0 <= target < len(candidates[index].neighbours):
                continue
            judgements[index] = Judgement("updates", target)
        else:
            judgements[index] = Judgement(verdict)  # type: ignore[arg-type]
    return [j if j is not None else COEXISTS for j in judgements]
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_memory_supersession.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/memory/supersession.py app/core/config.py tests/test_memory_supersession.py
git status
git commit -m "feat(memory): a judge decides same, updates or coexists [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Write-back uses the judge [S42]

**Files:**
- Modify: `app/services/memory.py` (`write_back`, new `_neighbours`), `tests/test_memory_lifecycle.py` (the two supersede tests now script the judge)
- Test: `tests/test_memory_lifecycle.py` (append)

**Interfaces:**
- Consumes: `supersession.judge`, `Candidate`, `Judgement`, `JUDGE_ROLE`; `Settings.memory_related_max_distance`.
- Produces: `write_back` behaviour per the spec; `_neighbours(session, learner_id, kind, embedding, *, max_distance, space, limit=3) -> list[Memory]` (current rows, nearest first).

- [ ] **Step 1: Failing tests.** In `tests/test_memory_lifecycle.py`, widen `_everything_is_equivalent` so both radii are wide:

```python
            lambda: Settings(memory_dedup_max_distance=2.0, memory_related_max_distance=2.0),
```

Add scripted replies at the top of the file:

```python
UPDATES_FIRST = '{"verdicts": [{"candidate": 1, "verdict": "updates", "replaces": 1}]}'
COEXISTS_FIRST = '{"verdicts": [{"candidate": 1, "verdict": "coexists"}]}'
SAME_FIRST = '{"verdicts": [{"candidate": 1, "verdict": "same"}]}'


def _extract_then_judge(extracted: str, verdicts: str):
    return fake_llm_client(script=[FakeTurn(text=extracted), FakeTurn(text=verdicts)])
```

(import `FakeTurn` from `app.llm.providers.fake`). In `test_a_correction_replaces_the_entry_it_contradicts` and `test_the_superseded_entry_is_kept_and_points_at_its_replacement`, replace the second write-back's `fake_llm_client(EVENINGS)` with `_extract_then_judge(EVENINGS, UPDATES_FIRST)`.

Append:

```python
# --- the judge decides (S42) ----------------------------------------------------------------


async def test_coexisting_memories_both_stay(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "I also study on Sundays.")

    with _everything_is_equivalent():
        await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, COEXISTS_FIRST), conversation_id=second.id
        )

    assert len(await svc.list_memories(db_session, learner.id)) == 2


async def test_a_rewording_is_skipped(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Mornings are when I study.")

    with _everything_is_equivalent():
        created = await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, SAME_FIRST), conversation_id=second.id
        )

    assert created == [] and len(await svc.list_memories(db_session, learner.id)) == 1


async def test_a_failed_judgement_keeps_both_and_still_advances(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Evenings now.")
    second_id = second.id

    with _everything_is_equivalent():
        await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, "garbage"), conversation_id=second_id
        )

    assert len(await svc.list_memories(db_session, learner.id)) == 2
    conversation = await db_session.get(Conversation, second_id, populate_existing=True)
    assert conversation is not None and conversation.memory_watermark is not None


async def test_an_identical_duplicate_needs_no_judge(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Mornings.")
    llm = _extract_then_judge(MORNINGS, UPDATES_FIRST)

    with _everything_is_equivalent():
        await svc.write_back(db_session, llm, conversation_id=second.id)

    assert len(llm._providers["fake"].prompts_sent) == 1, "only the extraction call"


async def test_summaries_are_never_judged(db_session: AsyncSession) -> None:
    summary = '{"memories": [{"kind": "summary", "content": "Covered vectors."}]}'
    other = '{"memories": [{"kind": "summary", "content": "Covered forces."}]}'
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "vectors")
    await svc.write_back(db_session, fake_llm_client(summary), conversation_id=first.id)
    second = await _conversation(db_session, learner, "forces")
    llm = _extract_then_judge(other, UPDATES_FIRST)

    with _everything_is_equivalent():
        await svc.write_back(db_session, llm, conversation_id=second.id)

    assert len(llm._providers["fake"].prompts_sent) == 1
    assert len(await svc.list_memories(db_session, learner.id)) == 2


async def test_two_candidates_naming_one_neighbour_supersede_it_once(
    db_session: AsyncSession,
) -> None:
    """Review focus 1: a second "updates" for a row already superseded in this run is stored
    as coexisting rather than superseding a non-current row."""
    two = (
        '{"memories": [{"kind": "preference", "content": "Evenings now."}, '
        '{"kind": "preference", "content": "Late evenings, actually."}]}'
    )
    both_update = (
        '{"verdicts": [{"candidate": 1, "verdict": "updates", "replaces": 1}, '
        '{"candidate": 2, "verdict": "updates", "replaces": 1}]}'
    )
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Evenings. No, late evenings.")

    with _everything_is_equivalent():
        await svc.write_back(
            db_session, _extract_then_judge(two, both_update), conversation_id=second.id
        )

    rows = await _memories(db_session, learner.id)
    superseded = [m for m in rows if m.status == MemoryStatus.SUPERSEDED]
    assert len(superseded) == 1
    assert len([m for m in rows if m.status == MemoryStatus.CURRENT]) == 2
```

(With both radii at 2.0, candidate 2 in the last test also sees candidate 1's neighbour set as it stood before the judge call — the original "mornings" row — which is exactly the collision under test.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_memory_lifecycle.py -q` → the new tests and the two re-scripted ones FAIL.

- [ ] **Step 3: Implement** in `app/services/memory.py`. Replace the body of the `for item, embedding in zip(...)` loop and what follows it with:

```python
    created: list[Memory] = []
    # Candidates the judge must decide: (item, embedding, neighbour rows).
    pending: list[tuple[ExtractedMemory, list[float], list[Memory]]] = []
    for item, embedding in zip(extracted, embedded.vectors, strict=True):
        if await _suppressed(
            session,
            conversation.learner_id,
            item.kind,
            embedding,
            conversation_id=conversation_id,
            max_distance=settings.memory_dedup_max_distance,
            space=space,
        ):
            # The learner forgot this. Re-extracting it is how a forgotten memory used to come
            # back; the tombstone is what stops that (scoped, since S42 decision B).
            continue
        prior = await _nearest(
            session,
            conversation.learner_id,
            item.kind,
            embedding,
            max_distance=settings.memory_dedup_max_distance,
            space=space,
            status=MemoryStatus.CURRENT,
        )
        if prior is not None and prior.content.strip() == item.content.strip():
            continue  # genuinely nothing new — no need to ask anyone
        neighbours = (
            []
            if item.kind == MemoryKind.SUMMARY  # what was covered always coexists
            else await _neighbours(
                session,
                conversation.learner_id,
                item.kind,
                embedding,
                max_distance=settings.memory_related_max_distance,
                space=space,
            )
        )
        if not neighbours:
            # Added now, so a later item in this batch sees it (autoflush makes it visible to
            # the next query) — intra-batch dedup, deliberately not batched.
            created.append(_new_memory(conversation, item, embedding, space))
            session.add(created[-1])
            continue
        pending.append((item, embedding, neighbours))

    if pending:
        judgements, usage = await supersession.judge(
            llm,
            [
                supersession.Candidate(item.content, [n.content for n in neighbours])
                for item, _embedding, neighbours in pending
            ],
        )
        if usage.total_tokens:
            await log_llm_call(
                learner_id=conversation.learner_id,
                role=supersession.JUDGE_ROLE.value,
                spec=llm.spec(supersession.JUDGE_ROLE),
                usage=usage,
                conversation_id=conversation_id,
            )
        for (item, embedding, neighbours), judgement in zip(pending, judgements, strict=True):
            if judgement.verdict == "same":
                continue
            memory = _new_memory(conversation, item, embedding, space)
            session.add(memory)
            created.append(memory)
            if judgement.verdict == "updates" and judgement.target is not None:
                old = neighbours[judgement.target]
                # Two candidates can name the same neighbour; only the first replaces it.
                if old.status == MemoryStatus.CURRENT:
                    await session.flush()
                    old.status = MemoryStatus.SUPERSEDED
                    old.superseded_by_id = memory.id
    await session.commit()
    return created
```

Add the helpers:

```python
def _new_memory(
    conversation: Conversation, item: ExtractedMemory, embedding: list[float], space: str
) -> Memory:
    return Memory(
        embedding_space=space,
        learner_id=conversation.learner_id,
        conversation_id=conversation.id,
        origin_conversation_id=conversation.id,
        kind=item.kind,
        content=item.content,
        embedding=embedding,
    )


async def _neighbours(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kind: MemoryKind,
    embedding: list[float],
    *,
    max_distance: float,
    space: str,
    limit: int = 3,
) -> list[Memory]:
    """The learner's current memories of ``kind`` close enough to be about the same thing,
    nearest first — what the judge compares a candidate against."""
    distance = exact_cosine_distance(Memory.embedding, embedding)
    rows = (
        await session.execute(
            select(Memory, distance.label("distance"))
            .where(
                Memory.learner_id == learner_id,
                Memory.kind == kind,
                Memory.embedding_space == space,
                Memory.status == MemoryStatus.CURRENT,
            )
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [memory for memory, dist in rows if dist <= max_distance]
```

and a temporary `_suppressed` that keeps today's learner-wide behaviour (Task 3 scopes it):

```python
async def _suppressed(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kind: MemoryKind,
    embedding: list[float],
    *,
    conversation_id: uuid.UUID,
    max_distance: float,
    space: str,
) -> bool:
    """Whether a forgotten memory stops this one being stored."""
    tombstone = await _nearest(
        session,
        learner_id,
        kind,
        embedding,
        max_distance=max_distance,
        space=space,
        status=MemoryStatus.DELETED,
    )
    return tombstone is not None
```

Imports: `from app.memory import supersession`, `from app.memory.extraction import EXTRACTION_ROLE, ExtractedMemory, extract_memories`. Update `write_back`'s docstring: near-duplicates are skipped for free; a candidate with current neighbours within `memory_related_max_distance` is judged same/updates/coexists (S42), doubt meaning coexists.

- [ ] **Step 4: Run** — `uv run pytest tests/test_memory_lifecycle.py tests/test_memory.py tests/test_removal.py tests/test_refresh_cursors.py tests/test_chat.py -q` → PASS; `uv run poe check && uv run poe format-check && uv run poe api-contract` → green. (If other write-back tests break because a second FAST call now returns the extraction text: that parses as no verdicts → coexists, which only changes tests that relied on auto-supersede — script them as in Step 1 and ledger each.)

- [ ] **Step 5: Commit**

```bash
git add app/services/memory.py tests/test_memory_lifecycle.py
git status
git commit -m "feat(memory): write-back asks the judge before replacing a memory [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Forget scope [S42]

**Files:**
- Create: `db/migrations/versions/0069_memory_forgotten_scope.py`
- Modify: `app/models/memory.py` (`forgotten_scope`, `ForgetScope`), `app/services/memory.py` (`_suppressed`, `delete_memory`, `delete_all_memories`), `app/services/removal.py` (both forget paths), `tests/test_migrations_with_data.py`
- Test: `tests/test_memory_lifecycle.py` (append)

**Interfaces:**
- Produces: `class ForgetScope(StrEnum): LEARNER = "learner"; CONVERSATION = "conversation"`; `Memory.forgotten_scope: str | None`.

- [ ] **Step 1: Failing tests** — append to `tests/test_memory_lifecycle.py`:

```python
# --- forget scope (S42 decision B) ---------------------------------------------------------


async def test_forgetting_a_conversation_suppresses_only_that_conversation(
    db_session: AsyncSession,
) -> None:
    from app.services import removal

    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    first_id = first.id
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first_id)
    await removal.forget_conversation_memories(db_session, learner.id, first_id)

    # The same conversation says it again: still suppressed.
    db_session.add(Message(conversation_id=first_id, role="user", content="Mornings, really."))
    await db_session.commit()
    assert await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first_id) == []

    # Another conversation teaches it: stored again.
    second = await _conversation(db_session, learner, "I study in the mornings.")
    created = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)
    assert [m.content for m in created] == ["Studies in the mornings."]


async def test_a_single_forget_suppresses_everywhere(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [memory] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    await svc.delete_memory(db_session, learner.id, memory.id)

    second = await _conversation(db_session, learner, "I study in the mornings.")
    assert await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id) == []


async def test_a_learner_wide_tombstone_wins_over_a_conversation_one(
    db_session: AsyncSession,
) -> None:
    """Review focus 3."""
    from app.services import removal

    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    await removal.forget_conversation_memories(db_session, learner.id, first.id)
    second = await _conversation(db_session, learner, "I study in the mornings.")
    [again] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)
    await svc.delete_memory(db_session, learner.id, again.id)

    third = await _conversation(db_session, learner, "I study in the mornings.")
    assert await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=third.id) == []


async def test_forget_everything_is_learner_wide(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    await svc.delete_all_memories(db_session, learner.id)
    rows = await _memories(db_session, learner.id)
    assert {m.forgotten_scope for m in rows} == {"learner"}
```

(The same text embeds identically with the fake provider, so the tombstone is within 0.05 without widening the threshold.)

Append to `tests/test_migrations_with_data.py` (reuse the INSERT shape of the slice A memories test in that file — it inserts a `memories` row with `status`):

```python
async def test_existing_forgotten_memories_stay_learner_wide() -> None:
    """0069 (S42): how an old tombstone was made is unknown, so it keeps today's reach."""
    async with database_at("0068_learner_preferences") as connect:
        conn = await connect()
        try:
            learner_id, deleted_id, current_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "scope"
            )
            for memory_id, status in ((deleted_id, "deleted"), (current_id, "current")):
                await conn.execute(
                    "INSERT INTO memories (id, learner_id, kind, content, embedding, "
                    "embedding_space, status) VALUES ($1, $2, 'fact', 'x', $3::vector, "
                    "'fake:fake-1:x', $4)",
                    memory_id,
                    learner_id,
                    "[" + ",".join(["0.1"] * get_settings().embed_dim) + "]",
                    status,
                )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0069_memory_forgotten_scope")

        conn = await connect()
        try:
            rows = {
                r["id"]: r["forgotten_scope"]
                for r in await conn.fetch(
                    "SELECT id, forgotten_scope FROM memories WHERE learner_id = $1", learner_id
                )
            }
            assert rows == {deleted_id: "learner", current_id: None}
        finally:
            await conn.close()
```

(Match the vector literal the slice A migration test uses in this file if it differs.)

- [ ] **Step 2: Run to verify failure** — the new tests FAIL (the conversation case is suppressed everywhere; no column).

- [ ] **Step 3: Implement.**

Migration `0069_memory_forgotten_scope.py` (`down_revision = "0068_learner_preferences"`):

```python
"""How far a forgotten memory's suppression reaches (S42, decision B).

``learner``: a single-memory Forget or "Forget everything" — never re-extracted from anywhere.
``conversation``: forgotten with the conversation it came from — re-extraction is suppressed
from that conversation only, so another conversation can teach it again. Existing tombstones
become ``learner``: how they were made is unknown, and that is today's behaviour.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069_memory_forgotten_scope"
down_revision: str | Sequence[str] | None = "0068_learner_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("forgotten_scope", sa.Text(), nullable=True))
    op.execute("UPDATE memories SET forgotten_scope = 'learner' WHERE status = 'deleted'")


def downgrade() -> None:
    op.drop_column("memories", "forgotten_scope")
```

`app/models/memory.py`:

```python
class ForgetScope(StrEnum):
    """How far a forgotten memory's suppression reaches (S42)."""

    LEARNER = "learner"  # a single Forget, or Forget everything
    CONVERSATION = "conversation"  # forgotten with the conversation it was learned in
```

and on `Memory`: `forgotten_scope: Mapped[str | None] = mapped_column(Text, default=None)` with a comment ("Set only on DELETED rows; see ForgetScope.").

`_suppressed` becomes a single query:

```python
    distance = exact_cosine_distance(Memory.embedding, embedding)
    row = (
        await session.execute(
            select(distance.label("distance"))
            .where(
                Memory.learner_id == learner_id,
                Memory.kind == kind,
                Memory.embedding_space == space,
                Memory.status == MemoryStatus.DELETED,
                or_(
                    Memory.forgotten_scope.is_(None),  # defensive: pre-0069 shape
                    Memory.forgotten_scope == ForgetScope.LEARNER,
                    and_(
                        Memory.forgotten_scope == ForgetScope.CONVERSATION,
                        Memory.origin_conversation_id == conversation_id,
                    ),
                ),
            )
            .order_by(distance)
            .limit(1)
        )
    ).first()
    return row is not None and row[0] <= max_distance
```

Docstring: "A learner-wide tombstone suppresses everywhere; a conversation-scoped one only re-extraction from the conversation it was learned in (decision B)."

Writers:
- `delete_memory`: `memory.forgotten_scope = ForgetScope.LEARNER` beside `status = DELETED`.
- `delete_all_memories`: `.values(status=MemoryStatus.DELETED, forgotten_scope=ForgetScope.LEARNER)`.
- `app/services/removal.py`, both `update(Memory)...values(status=MemoryStatus.DELETED)` → add `forgotten_scope=ForgetScope.CONVERSATION`.

- [ ] **Step 4: Run** — `uv run python -m tests.testdb`; `uv run pytest tests/test_memory_lifecycle.py tests/test_removal.py tests/test_memory.py tests/test_migrations_with_data.py -q` → PASS; `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/versions/0069_memory_forgotten_scope.py app/models/memory.py app/services/memory.py app/services/removal.py tests/test_memory_lifecycle.py tests/test_migrations_with_data.py
git status
git commit -m "feat(memory): forgetting a conversation suppresses only that conversation [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Undo a replacement, and show what was replaced [S42]

**Files:**
- Modify: `app/services/memory.py` (`NotAReplacement`, `undo_replacement`, `replaced_by`), `app/api/v1/memory.py` (route + `replaced` on list/correct responses), `app/schemas/memory.py` (`ReplacedRead`, `MemoryRead.replaced`), `frontend/src/api/schema.d.ts`
- Test: `tests/test_memory_lifecycle.py` (append), `tests/test_memory.py` or wherever the memory API is tested (`grep -rln '"/api/v1/memory' tests`)

**Interfaces:**
- Produces:
  ```python
  class NotAReplacement(Exception)
  async def undo_replacement(session, learner_id, memory_id) -> Memory | None  # the restored memory; None → 404
  async def replaced_by(session, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Memory]  # new id → the row it most recently superseded
  class ReplacedRead(BaseModel): id: uuid.UUID; content: str
  MemoryRead.replaced: ReplacedRead | None = None
  POST /api/v1/memory/{memory_id}/undo-replacement → MemoryRead (the restored memory)
  ```

- [ ] **Step 1: Failing tests** — append to `tests/test_memory_lifecycle.py`:

```python
# --- undo a replacement --------------------------------------------------------------------


async def _replaced_pair(db_session: AsyncSession, learner: Learner) -> tuple[uuid.UUID, uuid.UUID]:
    """(old id, new id) after "evenings" replaced "mornings"."""
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [old] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Evenings now.")
    with _everything_is_equivalent():
        [new] = await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, UPDATES_FIRST), conversation_id=second.id
        )
    return old.id, new.id


async def test_undo_restores_the_old_memory_and_retires_the_new(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    old_id, new_id = await _replaced_pair(db_session, learner)

    restored = await svc.undo_replacement(db_session, learner.id, new_id)

    assert restored is not None and restored.id == old_id
    old = await db_session.get(Memory, old_id, populate_existing=True)
    new = await db_session.get(Memory, new_id, populate_existing=True)
    assert old is not None and old.status == MemoryStatus.CURRENT and old.superseded_by_id is None
    assert new is not None and new.status == MemoryStatus.SUPERSEDED
    assert new.superseded_by_id == old_id and new.forgotten_scope is None


async def test_after_undo_the_statement_is_judged_afresh(db_session: AsyncSession) -> None:
    """Review focus 5: undo forgets nothing, so the statement is not suppressed."""
    learner = await _learner(db_session)
    _old_id, new_id = await _replaced_pair(db_session, learner)
    await svc.undo_replacement(db_session, learner.id, new_id)

    third = await _conversation(db_session, learner, "Evenings, as I said.")
    with _everything_is_equivalent():
        created = await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, COEXISTS_FIRST), conversation_id=third.id
        )
    assert [m.content for m in created] == ["Studies in the evenings now."]


async def test_undo_refuses_what_is_not_a_replacement(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [only] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    with pytest.raises(svc.NotAReplacement):
        await svc.undo_replacement(db_session, learner.id, only.id)
    assert await svc.undo_replacement(db_session, uuid.uuid4(), only.id) is None


async def test_undo_refuses_once_the_old_memory_has_changed(db_session: AsyncSession) -> None:
    """Review focus 2: the old row was forgotten since — nothing to restore."""
    learner = await _learner(db_session)
    old_id, new_id = await _replaced_pair(db_session, learner)
    old = await db_session.get(Memory, old_id, populate_existing=True)
    assert old is not None
    old.status = MemoryStatus.DELETED
    old.forgotten_scope = "learner"
    await db_session.commit()

    with pytest.raises(svc.NotAReplacement):
        await svc.undo_replacement(db_session, learner.id, new_id)


async def test_undo_reverts_a_correction(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [original] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    original_id = original.id
    corrected = await svc.correct_memory(
        db_session, fake_llm_client(), learner.id, original_id, content="Studies at night."
    )
    assert corrected is not None

    restored = await svc.undo_replacement(db_session, learner.id, corrected.id)

    assert restored is not None and restored.id == original_id
```

API test (in the memory API test file, using `api_client`/`api_learner` and seeding rows directly so no model call is needed):

```python
async def test_the_list_says_what_a_memory_replaced_and_undo_works_over_the_route(
    api_client, db_session, api_learner
) -> None:
    old = Memory(
        embedding_space=FAKE_SPACE, learner_id=api_learner.id, kind="preference",
        content="Studies in the mornings.", embedding=[0.1] * 768,
    )
    new = Memory(
        embedding_space=FAKE_SPACE, learner_id=api_learner.id, kind="preference",
        content="Studies in the evenings now.", embedding=[0.2] * 768,
    )
    db_session.add_all([old, new])
    await db_session.flush()
    old.status, old.superseded_by_id = MemoryStatus.SUPERSEDED, new.id
    await db_session.flush()

    listed = (await api_client.get("/api/v1/memory")).json()
    [entry] = [m for m in listed if m["id"] == str(new.id)]
    assert entry["replaced"] == {"id": str(old.id), "content": "Studies in the mornings."}

    r = await api_client.post(f"/api/v1/memory/{new.id}/undo-replacement")
    assert r.status_code == 200 and r.json()["id"] == str(old.id)
    again = await api_client.post(f"/api/v1/memory/{new.id}/undo-replacement")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_a_replacement"
    assert (await api_client.post(f"/api/v1/memory/{uuid.uuid4()}/undo-replacement")).status_code == 404
```

(Use the embedding dimension from settings, `[0.1] * get_settings().embed_dim`, if 768 is not it; import `FAKE_SPACE` from `tests.embedding`.)

- [ ] **Step 2: Run to verify failure** — FAIL (`undo_replacement` missing; no `replaced`; route 404/405).

- [ ] **Step 3: Implement.**

`app/services/memory.py`:

```python
class NotAReplacement(Exception):
    """Undo asked of a memory that replaced nothing still restorable."""


async def replaced_by(
    session: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Memory]:
    """For each id, the superseded memory it most recently replaced (if any)."""
    if not ids:
        return {}
    rows = (
        await session.scalars(
            select(Memory)
            .where(
                Memory.superseded_by_id.in_(ids), Memory.status == MemoryStatus.SUPERSEDED
            )
            .order_by(Memory.created_at.desc())
        )
    ).all()
    found: dict[uuid.UUID, Memory] = {}
    for row in rows:
        assert row.superseded_by_id is not None
        found.setdefault(row.superseded_by_id, row)
    return found


async def undo_replacement(
    session: AsyncSession, learner_id: uuid.UUID, memory_id: uuid.UUID
) -> Memory | None:
    """Put back what ``memory_id`` replaced; retire ``memory_id`` without forgetting it (S42).

    ``None`` when the id is not this learner's. ``NotAReplacement`` when it is not current, or
    nothing it replaced is still superseded by it — the old row was since corrected, forgotten
    or already restored. Nothing is suppressed: the retired statement, said again, is judged
    afresh.
    """
    memory = await session.get(Memory, memory_id, with_for_update=True)
    if memory is None or memory.learner_id != learner_id:
        return None
    if memory.status != MemoryStatus.CURRENT:
        raise NotAReplacement(str(memory_id))
    old = (await replaced_by(session, [memory_id])).get(memory_id)
    if old is None:
        raise NotAReplacement(str(memory_id))
    old.status = MemoryStatus.CURRENT
    old.superseded_by_id = None
    await session.flush()
    memory.status = MemoryStatus.SUPERSEDED
    memory.superseded_by_id = old.id
    await session.commit()
    await session.refresh(old)
    return old
```

`app/schemas/memory.py`:

```python
class ReplacedRead(BaseModel):
    """The memory a current one replaced (S42) — shown so a wrong replacement can be undone."""

    id: uuid.UUID
    content: str
```

and `MemoryRead.replaced: ReplacedRead | None = None`.

`app/api/v1/memory.py`: in `list_memory`, after `memories = ...`, `replaced = await svc.replaced_by(session, [m.id for m in memories])` and add `"replaced": ReplacedRead(id=r.id, content=r.content) if (r := replaced.get(m.id)) else None` to each `model_copy` update. The correction route returns the new memory — also fill `replaced` for it (one `replaced_by` call). New route:

```python
@router.post("/memory/{memory_id}/undo-replacement", response_model=MemoryRead)
async def undo_replacement(memory_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Put back what this memory replaced — for when a replacement was wrong (S42)."""
    try:
        restored = await svc.undo_replacement(session, learner.id, memory_id)
    except svc.NotAReplacement as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "not_a_replacement", "message": "This memory did not replace anything."},
        ) from exc
    if restored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
    return restored
```

(`memory_id` is a Memory id; the visibility sweep's `_NOT_GRAPH_IDS` already lists `memory_id`.)

- [ ] **Step 4: Run** — memory tests → PASS; `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/memory.py app/api/v1/memory.py app/schemas/memory.py frontend/src/api/schema.d.ts tests/test_memory_lifecycle.py <the memory API test file>
git status
git commit -m "feat(memory): a replaced memory is shown and can be put back [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Frontend — "Replaced" and Undo on the Memory page [S42]

**Files:**
- Modify: `frontend/src/api/hooks.ts` (`useUndoReplacement`), `frontend/src/pages/Memory.tsx` (`Row`), `frontend/src/pages/Memory.test.tsx`

**Interfaces:**
- Consumes: `MemoryRead.replaced`, `POST /api/v1/memory/{memory_id}/undo-replacement`.
- Produces: `useUndoReplacement()` (mutation taking the memory id; invalidates `["memories"]`).

- [ ] **Step 1: Failing test** — append to `Memory.test.tsx`, in its fetch-stub style:

```tsx
describe("a replacement that was wrong", () => {
  it("shows what was replaced and puts it back on Undo", async () => {
    const replacing = {
      ...MEMORY,
      id: "m-new",
      content: "Studies in the evenings now",
      replaced: { id: "m-old", content: "Studies in the mornings" },
    };
    const fetchMock = vi.fn((input: Request | string) => {
      const request = input as Request;
      if (request.method === "POST") {
        return Promise.resolve(jsonResponse({ ...MEMORY, id: "m-old" }));
      }
      return Promise.resolve(jsonResponse([replacing]));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    expect(await screen.findByText(/Replaced: Studies in the mornings/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Undo replacement/ }));

    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(([r]) => (r as Request).method === "POST");
      expect(posts).toHaveLength(1);
      expect((posts[0][0] as Request).url).toContain("/api/v1/memory/m-new/undo-replacement");
    });
  });
});
```

- [ ] **Step 2: Run to verify failure** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/pages/Memory.test.tsx` → FAIL.

- [ ] **Step 3: Implement.**

`hooks.ts`, beside `useForgetMemory`:

```ts
/** Put back the memory this one replaced (S42) — for when an automatic replacement was
 * wrong. Nothing is forgotten, so the retired statement can be learned again later. */
export function useUndoReplacement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.POST("/api/v1/memory/{memory_id}/undo-replacement", {
        params: { path: { memory_id: id } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memories"] });
    },
  });
}
```

`Memory.tsx` `Row`: `const undo = useUndoReplacement();` and, just above `<Origin memory={memory} />`:

```tsx
      {memory.replaced && (
        <p className="text-caption text-base-content/60 pl-24">
          Replaced: {memory.replaced.content}{" "}
          <button
            type="button"
            className="link"
            disabled={undo.isPending}
            onClick={() => undo.mutate(memory.id)}
            aria-label={`Undo replacement of: ${memory.replaced.content}`}
          >
            Undo
          </button>
        </p>
      )}
      {undo.isError && (
        <p className="text-caption text-error pl-24">
          That can&apos;t be undone any more — the earlier memory changed since.
        </p>
      )}
```

(Match the row's existing caption/indent classes.)

- [ ] **Step 4: Run** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build && npm run lint` → green; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/hooks.ts frontend/src/pages/Memory.tsx frontend/src/pages/Memory.test.tsx
git status
git commit -m "feat(web): see a replaced memory and put it back [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Tracker and CLAUDE.md [S42]

- [ ] **Step 1:** Tracker: move S42 to "Completed and consolidated work" (sorted by id) as **Implemented**: "Correction, forgetting and suppression are durable. Supersession is an explicit same/updates/coexists judgement (one batched FAST call per write-back, only for candidates with near neighbours; doubt means coexists), shown on the Memory page as 'Replaced: …' with Undo. Forgetting a conversation suppresses re-extraction only from that conversation; a single Forget and Forget everything stay learner-wide. Remaining for the testing phase: calibrate `memory_related_max_distance` and measure the judge." Evidence: `app/memory/supersession.py`, `app/services/memory.py`, `tests/test_memory_supersession.py`, `tests/test_memory_lifecycle.py`. Add any deferred minors from the final review.
- [ ] **Step 2:** CLAUDE.md: extend the "Archive, delete and forget" bullet's last sentence with: "Forgetting a conversation suppresses its facts only from that conversation; a replaced memory is an explicit judgement (`app/memory/supersession.py`), shown and undoable."
- [ ] **Step 3:** `uv run poe check` → green; commit:

```bash
git add docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record memory supersession and forget scope [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
