# Declared-Check Criteria Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A conversational check without a rubric gets criteria and a rated difficulty from one FAST call on its first real attempt, and the tutor is told what level to pitch declared checks at.

**Architecture:** `difficulty` gains the band list and midpoints; `item_generation.write_criteria` is the pure model call; a new `app/services/check_criteria.py::ensure_criteria` locks the item, writes the rubric and difficulty, and is called from `chat._resolve_check` between the intent gate and `answer_item`. `chat` appends a level sentence (from the subject roll-up) to the declared-check instruction.

**Tech Stack:** Python 3.13, SQLAlchemy async (`refresh(..., with_for_update=True)`), pytest, FakeProvider scripts.

**Spec:** `docs/superpowers/specs/2026-09-28-declared-check-criteria-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check` and `uv run poe format-check`. No migration in this plan.
- One tracker id per commit subject: `[S56]`. Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls: tests use `FakeProvider` scripts.
- The criteria call never sees the learner's answer. Logs carry ids only.

## Deviations from the spec's wording (decided here)

- `ensure_criteria` lives in a new `app/services/check_criteria.py`, not `app/services/assessment.py`: `item_generation` already imports `assessment`, so the reverse import would be a cycle.
- A refused criteria call makes `_resolve_check` return `(item, None)` — check open, nothing recorded — which is how it already treats a failed grade. The tutor's own call in the same turn meets the same refusal and ends the turn through `refusal_ends_turn`, so the learner-visible outcome is the spec's.

## Review Focus

1. Two first attempts at the same rubric-less check racing: the second must wait on the row lock and reuse the first's rubric — never two rubrics, never a second criteria call (Task 2, by construction; reviewer to check the lock is taken before the call).
2. A retried turn (same `attempt_id`) after the first try wrote criteria and a grade: no second criteria call, the replayed grade is unchanged (Task 2 test).
3. The criteria call refused by the spend guard: no event, no rubric, the check stays open (Task 2 test).
4. The criteria reply parses but names an unknown level: criteria kept, difficulty unchanged (Task 2 test).
5. A plan-posed check that already has a rubric: no criteria call, nothing changed (Task 2 test).

---

### Task 1: Bands and the criteria call [S56]

**Files:**
- Modify: `app/learning/difficulty.py`, `app/learning/item_generation.py`
- Test: `tests/test_check_criteria.py` (create)

**Interfaces:**
- Produces:
  ```python
  # app/learning/difficulty.py
  LEVELS: tuple[tuple[str, str], ...]          # (band name, gloss), easiest first
  def midpoint(level: str) -> float            # ValueError for an unknown name
  # app/learning/item_generation.py
  CRITERIA_SYSTEM_PROMPT: str
  async def write_criteria(llm, *, stem: str, component_name: str,
                           component_description: str = "", max_tokens: int = 256
                           ) -> tuple[list[str], str | None, Usage]
  ```

- [ ] **Step 1: Failing tests** — `tests/test_check_criteria.py`:

```python
"""A conversational check gets a standard and a level before it is graded (S56)."""

import json

import pytest

from app.learning import difficulty, item_generation
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole

CRITERIA = json.dumps({"criteria": ["names both parts", "gives a reason"], "level": "challenging"})


def _client(*replies: str) -> tuple[LLMClient, FakeProvider]:
    from app.llm.providers.fake import FakeTurn

    provider = FakeProvider(reply="", script=[FakeTurn(text=r) for r in replies])
    specs = {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}
    return LLMClient({"fake": provider}, specs), provider


@pytest.mark.parametrize("level", [name for name, _gloss in difficulty.LEVELS])
def test_each_level_has_a_midpoint_inside_its_own_band(level: str) -> None:
    assert difficulty.band(difficulty.midpoint(level)) == level


def test_the_midpoints_are_one_logit_apart() -> None:
    assert [difficulty.midpoint(n) for n, _ in difficulty.LEVELS] == [-2.0, -1.0, 0.0, 1.0, 2.0]


def test_an_unknown_level_has_no_midpoint() -> None:
    with pytest.raises(ValueError):
        difficulty.midpoint("impossible")


async def test_criteria_and_level_are_read_from_the_reply() -> None:
    llm, provider = _client(CRITERIA)
    criteria, level, _ = await item_generation.write_criteria(
        llm, stem="Why is momentum a vector?", component_name="Momentum"
    )
    assert criteria == ["names both parts", "gives a reason"]
    assert level == "challenging"
    [(system, messages)] = provider.prompts_sent
    assert system == item_generation.CRITERIA_SYSTEM_PROMPT
    assert "Why is momentum a vector?" in str(messages[0].content)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("not json", ([], None)),
        (json.dumps({"criteria": ["a"], "level": "impossible"}), (["a"], None)),
        (json.dumps({"criteria": [" ", "b"], "level": " Moderate "}), (["b"], "moderate")),
        (json.dumps({"level": "moderate"}), ([], "moderate")),
    ],
)
async def test_a_partial_reply_keeps_what_it_can(reply: str, expected: tuple) -> None:
    llm, _ = _client(reply)
    criteria, level, _ = await item_generation.write_criteria(llm, stem="Q", component_name="K")
    assert (criteria, level) == expected
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_check_criteria.py -q` → FAIL (`LEVELS` / `write_criteria` missing).

- [ ] **Step 3: Implement.**

`app/learning/difficulty.py`, after `_BANDS`:

```python
LEVELS: tuple[tuple[str, str], ...] = tuple((name, gloss) for _, name, gloss in _BANDS)
"""The band names and what each means, easiest first — what a model is asked to choose from."""

_MIDPOINTS: dict[str, float] = {"introductory": -2.0, "straightforward": -1.0, "moderate": 0.0,
                                "challenging": 1.0, "demanding": 2.0}


def midpoint(level: str) -> float:
    """The difficulty recorded for a question *rated* at ``level`` (S56): the middle of its
    one-logit band (the outer two, one logit past the last boundary). A rating, not a
    measurement — see ``app.learning.item_generation`` on what a stored difficulty claims."""
    try:
        return _MIDPOINTS[level]
    except KeyError:
        raise ValueError(f"unknown level {level!r}") from None
```

(Format with ruff; check `_MIDPOINTS`'s keys equal `[name for name, _ in LEVELS]`.)

`app/learning/item_generation.py` — after `_parse_short`'s section, add:

```python
CRITERIA_SYSTEM_PROMPT = (
    "You write the marking criteria for one short-answer question a tutor has already asked, "
    "and say how hard the question is. Respond with ONLY a JSON object "
    '{"criteria": ["<what a full-credit answer must show>", ...], "level": "<level>"} and '
    "nothing else. Give two to four criteria, each one specific and checkable. The level is "
    "exactly one of: "
    + "; ".join(f"{name} ({gloss})" for name, gloss in difficulty_mod.LEVELS)
    + "."
)


async def write_criteria(
    llm: LLMClient,
    *,
    stem: str,
    component_name: str,
    component_description: str = "",
    max_tokens: int = 256,
) -> tuple[list[str], str | None, Usage]:
    """Criteria and a rated level for a question that already exists (S56).

    For a conversational check the tutor declared, or a posed check whose generated criteria
    did not parse. Given the question and its component only — never a learner's answer, so
    the standard is fixed before anything is graded against it. Both halves are best-effort:
    no usable criteria is ``[]``, and a level that is not exactly a band name is ``None``.
    """
    component = component_name + (f" — {component_description}" if component_description else "")
    completion = await llm.complete(
        GENERATION_ROLE,
        [
            ChatMessage(
                role=ChatRole.USER,
                content=f"Question:\n{stem}\n\nKnowledge component: {component}",
            )
        ],
        system=CRITERIA_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    criteria, level = _parse_criteria(completion.content)
    return criteria, level, completion.usage


def _parse_criteria(content: str) -> tuple[list[str], str | None]:
    try:
        raw = json.loads(_extract_json(content))
    except (json.JSONDecodeError, TypeError, ValueError):
        return [], None
    if not isinstance(raw, dict):
        return [], None
    listed = raw.get("criteria")
    criteria = (
        [text for text in (str(c).strip() for c in listed) if text]
        if isinstance(listed, list)
        else []
    )
    level = str(raw.get("level", "")).strip().lower()
    names = {name for name, _gloss in difficulty_mod.LEVELS}
    return criteria, level if level in names else None
```

(Check `_extract_json`'s behaviour on "not json" — if it raises something other than the three caught, add it to the tuple.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_check_criteria.py tests/test_difficulty_targeting.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/difficulty.py app/learning/item_generation.py tests/test_check_criteria.py
git status
git commit -m "feat(checks): criteria and a rated level for an existing question [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: A check gets its criteria on its first attempt [S56]

**Files:**
- Create: `app/services/check_criteria.py`
- Modify: `app/services/chat.py` (`_resolve_check`; `_materialise_declared_check` docstring)
- Test: `tests/test_check_criteria.py` (append)

**Interfaces:**
- Consumes: `write_criteria`, `difficulty.midpoint` (Task 1).
- Produces: `async def ensure_criteria(session, llm, learner_id: uuid.UUID, item: Item) -> Item` — the same `Item` object, with `rubric`/`rubric_id`/`difficulty` updated and flushed.

- [ ] **Step 1: Failing tests.** Append to `tests/test_check_criteria.py` (move the new imports to the top of the file):

```python
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import declared_check
from app.llm.meter import BudgetExceeded
from app.models.chat import Conversation, ConversationPhase
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services import chat as chat_svc

ATTEMPT = '{"intent": "attempt"}'
DEFERRAL = '{"intent": "deferral"}'
GRADE = '{"score": 0.9, "rationale": "ok"}'
ANSWER = "Because it has a direction as well as a size."


async def _declared(session: AsyncSession):
    learner = Learner(handle=f"cc-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="momentum", name="Momentum", description="mass times velocity")
    session.add(kc)
    await session.flush()
    item = await chat_svc._materialise_declared_check(
        session,
        learner_id=learner.id,
        subject_id=subject.id,
        declared=declared_check.DeclaredCheck(
            component="Momentum", question="Why is momentum a vector?"
        ),
    )
    assert item is not None and item.rubric_id is None
    conversation = Conversation(
        learner_id=learner.id,
        subject_id=subject.id,
        phase=ConversationPhase.AWAITING_ANSWER,
        active_item_id=item.id,
    )
    session.add(conversation)
    await session.commit()
    return learner, kc, conversation, item


async def _answer(session, llm, learner, conversation, text=ANSWER, attempt_id=None):
    return await chat_svc._resolve_check(
        session,
        llm,
        learner_id=learner.id,
        conversation=conversation,
        user_content=text,
        attempt_id=attempt_id,
    )


def _criteria_calls(provider: FakeProvider) -> list:
    return [m for s, m in provider.prompts_sent if s == item_generation.CRITERIA_SYSTEM_PROMPT]


async def _events(session, learner_id):
    return list(
        (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.learner_id == learner_id)
            )
        ).all()
    )


async def test_the_first_attempt_writes_criteria_and_a_rated_level(db_session) -> None:
    learner, kc, conversation, item = await _declared(db_session)
    llm, provider = _client(ATTEMPT, CRITERIA, GRADE)

    still_open, outcome = await _answer(db_session, llm, learner, conversation)

    assert still_open is None and outcome is not None
    assert item.rubric is not None and item.rubric.kc_id == kc.id
    assert item.rubric.criteria == {"criteria": ["names both parts", "gives a reason"]}
    assert item.rubric.owner_learner_id == learner.id
    assert item.difficulty == 1.0
    [event] = await _events(db_session, learner.id)
    assert event.payload["difficulty"] == 1.0
    assert event.payload["grading"]["rubric"] is not None
    assert len(_criteria_calls(provider)) == 1


async def test_the_criteria_call_never_sees_the_answer(db_session) -> None:
    learner, _kc, conversation, _item = await _declared(db_session)
    llm, provider = _client(ATTEMPT, CRITERIA, GRADE)
    await _answer(db_session, llm, learner, conversation)
    [messages] = _criteria_calls(provider)
    assert all(ANSWER not in str(m.content) for m in messages)


async def test_a_second_attempt_reuses_the_criteria(db_session) -> None:
    """Review focus 2 as well: a retried turn with the same attempt id."""
    learner, _kc, conversation, item = await _declared(db_session)
    attempt = uuid.uuid4()
    await _answer(db_session, _client(ATTEMPT, CRITERIA, GRADE)[0], learner, conversation,
                  attempt_id=attempt)
    rubric_id = item.rubric_id

    llm, provider = _client(ATTEMPT, GRADE)
    _open, outcome = await _answer(db_session, llm, learner, conversation, attempt_id=attempt)

    assert outcome is not None and outcome.result.score == 0.9
    assert _criteria_calls(provider) == [] and item.rubric_id == rubric_id
    assert len(await _events(db_session, learner.id)) == 1


async def test_a_deferral_writes_no_criteria(db_session) -> None:
    learner, _kc, conversation, item = await _declared(db_session)
    llm, provider = _client(DEFERRAL)
    still_open, outcome = await _answer(db_session, llm, learner, conversation, text="hint?")
    assert still_open is not None and outcome is None
    assert _criteria_calls(provider) == [] and item.rubric_id is None


async def test_an_unusable_reply_grades_the_old_way(db_session) -> None:
    learner, _kc, conversation, item = await _declared(db_session)
    llm, provider = _client(ATTEMPT, "not json", GRADE)
    _open, outcome = await _answer(db_session, llm, learner, conversation)
    assert outcome is not None
    assert item.rubric_id is None and item.difficulty == 0.0


async def test_an_unknown_level_keeps_the_criteria_only(db_session) -> None:
    """Review focus 4."""
    learner, _kc, conversation, item = await _declared(db_session)
    reply = json.dumps({"criteria": ["a"], "level": "impossible"})
    _open, outcome = await _answer(db_session, _client(ATTEMPT, reply, GRADE)[0], learner,
                                   conversation)
    assert outcome is not None and item.rubric is not None and item.difficulty == 0.0


async def test_a_check_with_criteria_is_left_alone(db_session) -> None:
    """Review focus 5."""
    learner, _kc, conversation, item = await _declared(db_session)
    await _answer(db_session, _client(ATTEMPT, CRITERIA, GRADE)[0], learner, conversation)
    before = (item.rubric_id, item.difficulty)
    llm, provider = _client(ATTEMPT, GRADE)
    await _answer(db_session, llm, learner, conversation)
    assert _criteria_calls(provider) == [] and (item.rubric_id, item.difficulty) == before


async def test_a_refused_criteria_call_records_nothing(db_session) -> None:
    """Review focus 3."""

    class Refusing(FakeProvider):
        async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
            if system == item_generation.CRITERIA_SYSTEM_PROMPT:
                raise BudgetExceeded("learner")
            return await super().complete(
                model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
            )

    learner, _kc, conversation, item = await _declared(db_session)
    provider = Refusing(reply=ATTEMPT)
    specs = {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}
    still_open, outcome = await _answer(
        db_session, LLMClient({"fake": provider}, specs), learner, conversation
    )
    assert still_open is not None and outcome is None
    assert item.rubric_id is None and await _events(db_session, learner.id) == []


async def test_a_failed_criteria_call_still_grades(db_session) -> None:
    class Failing(FakeProvider):
        async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
            if system == item_generation.CRITERIA_SYSTEM_PROMPT:
                raise RuntimeError("provider down")
            return await super().complete(
                model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
            )

    from app.llm.providers.fake import FakeTurn

    learner, _kc, conversation, item = await _declared(db_session)
    provider = Failing(reply="", script=[FakeTurn(text=ATTEMPT), FakeTurn(text=GRADE)])
    specs = {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}
    _open, outcome = await _answer(
        db_session, LLMClient({"fake": provider}, specs), learner, conversation
    )
    assert outcome is not None and item.rubric_id is None
```

(Notes for the executor: the failing provider's script index still advances only on the calls that reach `super().complete`, so its script is gate → grade. Jev decisions are off in tests by default, so the intent gate is the FAST `complete` call and is first. If the SMART grader's user message is built before the criteria call, the order still holds: gate, criteria, grade.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_check_criteria.py -q` → the new tests FAIL (no criteria written; `item.difficulty` 0.0).

- [ ] **Step 3: Implement.**

`app/services/check_criteria.py`:

```python
"""Criteria and a rated level for a check that has none, written on its first attempt (S56).

A check the tutor declares in conversation is an item with no rubric and no judged difficulty
(``chat._materialise_declared_check``); a posed check whose generated criteria did not parse
is the same. Writing them when the check is declared would pay for questions nobody answers,
and letting the tutor write them into its marker would stream the answer onto the learner's
screen. So they are written here, once, when an answer has passed the intent gate and before
it is graded — from the question alone, never from the answer.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import difficulty, item_generation
from app.llm import LLMClient
from app.llm.attribution import metered
from app.models.assessment import Item, Rubric
from app.models.knowledge import KC

log = structlog.get_logger()


@metered("check_criteria", learner="learner_id")
async def ensure_criteria(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, item: Item
) -> Item:
    """Give ``item`` criteria and a rated difficulty if it has no rubric yet; returns it.

    The row is locked first, so two first attempts racing cannot both write: the second waits,
    finds the first one's rubric, and makes no call. A reply without usable criteria leaves
    the item as it was — it is then graded against the grader's fallback, as before — and a
    level that is not a band name leaves the difficulty alone.
    """
    await session.refresh(item, ["rubric_id"], with_for_update=True)
    if item.rubric_id is not None:
        await session.refresh(item, ["rubric"])
        return item
    kc = await session.get(KC, item.kc_links[0].kc_id) if item.kc_links else None
    if kc is None:
        return item
    criteria, level, _usage = await item_generation.write_criteria(
        llm, stem=item.stem, component_name=kc.name, component_description=kc.description or ""
    )
    if criteria:
        rubric = Rubric(
            kc_id=kc.id,
            owner_learner_id=learner_id,
            name="Criteria for a conversational check",
            criteria={"criteria": criteria},
        )
        session.add(rubric)
        await session.flush()
        item.rubric_id = rubric.id
        item.rubric = rubric
    else:
        log.info("check.criteria_unavailable", item_id=str(item.id))
    if level is not None:
        item.difficulty = difficulty.midpoint(level)
    await session.flush()
    return item
```

`app/services/chat.py::_resolve_check` — after the `DEFERRAL` branch and before `kc_ids = ...`:

```python
    if item.rubric_id is None:
        # A check the tutor declared has no standard yet; write one now that it is being
        # answered, from the question alone (S56). Failing to is not a reason to lose the
        # answer — it is graded against the fallback, as before — but a refusal is: the turn
        # is over budget, so nothing is recorded and the question stays open.
        try:
            item = await check_criteria_svc.ensure_criteria(session, llm, learner_id, item)
        except BudgetExceeded:
            return item, None
        except Exception as exc:
            log.warning(
                "check.criteria_unavailable", item_id=str(item.id), error=type(exc).__name__
            )
```

Import `from app.services import check_criteria as check_criteria_svc` and `from app.llm.meter import BudgetExceeded` (if not already imported). In `_materialise_declared_check`'s docstring, replace the paragraph starting "No rubric, so it is graded by ``grade_open``'s stated fallback." with:

```
    No rubric yet: criteria and a rated difficulty are written on its first real attempt
    (``app.services.check_criteria``, S56), so a question nobody answers costs nothing.
```

and in the paragraph above it change "it has no reviewed rubric, no difficulty target" to "it has no reviewed rubric".

- [ ] **Step 4: Run** — `uv run pytest tests/test_check_criteria.py tests/test_declared_check.py tests/test_conversation_evidence.py tests/test_grading_provenance.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/check_criteria.py app/services/chat.py tests/test_check_criteria.py
git status
git commit -m "feat(checks): a check without criteria gets them on its first attempt [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Aim the tutor's declared checks [S56]

**Files:**
- Modify: `app/learning/declared_check.py`, `app/services/chat.py`
- Test: `tests/test_check_criteria.py` (append)

**Interfaces:**
- Produces: `declared_check.instruction(level: str) -> str`; `chat._declared_check_note(session, *, learner_id, subject_id) -> str`.

- [ ] **Step 1: Failing tests** (append; imports to the top):

```python
from app.learning import mastery
from app.learning.tracer import Estimate


def test_the_instruction_names_the_level() -> None:
    note = declared_check.instruction("moderate (two or three steps)")
    assert note.startswith(declared_check.INSTRUCTION)
    assert note.endswith("Pitch any check at this level: moderate (two or three steps).")


@pytest.mark.parametrize(("ability", "level"), [(-3.0, "introductory"), (3.0, "demanding")])
async def test_the_level_follows_the_learners_subject_estimate(
    db_session, monkeypatch, ability: float, level: str
) -> None:
    async def rollup(*_args, **_kwargs) -> Estimate:
        return Estimate(ability=ability, uncertainty=0.5)

    monkeypatch.setattr(mastery, "rollup_subject", rollup)
    note = await chat_svc._declared_check_note(
        db_session, learner_id=uuid.uuid4(), subject_id=uuid.uuid4()
    )
    assert f"Pitch any check at this level: {level} (" in note
```

(`practice_target` uses `practice_target_success_rate`; with its default the two abilities land in the outer bands. If the default moves them inward, use ±4.0.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_check_criteria.py -q` → FAIL (`instruction` / `_declared_check_note` missing).

- [ ] **Step 3: Implement.**

`app/learning/declared_check.py`, after `INSTRUCTION`:

```python
def instruction(level: str) -> str:
    """``INSTRUCTION`` plus the level to pitch a check at (S56) — a band description from
    ``app.learning.difficulty``, never a number: a model cannot act on a logit."""
    return f"{INSTRUCTION} Pitch any check at this level: {level}."
```

`app/services/chat.py`:

```python
async def _declared_check_note(
    session: AsyncSession, *, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> str:
    """The invitation to declare a check, aimed at this learner (S56).

    The component is not known until the tutor names it, so the aim is the practice target
    for the learner's estimate over the whole subject — the same target plan-driven items use.
    It steers; it is not recorded. What is recorded is the level the question is rated at when
    it is first answered (``app.services.check_criteria``).
    """
    estimate = await mastery.rollup_subject(session, learner_id, subject_id)
    return declared_check.instruction(
        difficulty.describe(session_runner_svc.practice_target(estimate))
    )
```

and in `run_tutor_turn` replace `notes.append(declared_check.INSTRUCTION)` with
`notes.append(await _declared_check_note(session, learner_id=learner_id, subject_id=subject_id))`.
Import `difficulty` from `app.learning` if not present. Check any existing test that asserts `declared_check.INSTRUCTION in system` still passes (it does: the note starts with it).

- [ ] **Step 4: Run** — `uv run pytest tests/test_check_criteria.py tests/test_declared_check.py tests/test_conversation_evidence.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/declared_check.py app/services/chat.py tests/test_check_criteria.py
git status
git commit -m "feat(checks): the tutor is told what level to pitch a declared check at [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Docs [S56]

- [ ] **Step 1: Tracker** — S56 row: status `Implemented`; the declared-check part done (criteria and a rated difficulty written on a check's first attempt by `app/services/check_criteria.py`, the tutor aimed with a level from the subject roll-up); note the optional Jev difficulty-band shadow question as later work; add the spec link and the final review's deferred minors. Update the tracker header's summary if it counts open S-items.
- [ ] **Step 2: Commit**

```bash
git add docs/guru-suggestions-tracker.md
git status
git commit -m "docs: S56 closes with criteria for conversational checks [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
