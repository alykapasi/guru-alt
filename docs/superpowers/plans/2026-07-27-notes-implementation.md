# Phase 8 — Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-learner, per-topic notes that grow unprompted from study activity — a format-neutral
atom substrate distilled on read, projected into learner-chosen formats, with revision history.

**Architecture:** A meta-wiki substrate (JSONB list of KC-tagged atoms) is the source of truth;
rendered notes are cached LLM projections of it. Catch-up-on-read distillation keyed by a per-note
watermark; a code-enforced invariant protects learner-contributed atoms; append-only revisions make
every change recoverable. Spec: `docs/superpowers/specs/2026-07-27-notes-design.md`.

**Tech Stack:** SQLAlchemy 2 async + Alembic (migration `0017`), FastAPI, role-based `LLMClient`
(SMART), pytest + `fake_llm_client`, React 19 + TanStack Query + `react-markdown` + DaisyUI.

## Global Constraints

- All LLM calls go through the role registry: `NOTES_ROLE = ModelRole.SMART` — never a provider
  SDK, never a hardcoded model name (CLAUDE.md).
- Every LLM call is cost-logged via `app.services.llm_log.log_llm_call(session, learner_id=…,
  role=…, spec=llm.spec(ROLE), usage=…)` — flushes, caller commits.
- Timestamps compared against `LearningEvent.created_at` / `Message.created_at` must be **naive
  UTC** (`datetime.now(UTC).replace(tzinfo=None)`) — the tz-aware/naive asyncpg crash is a known,
  documented precedent (`app/learning/activity.py`).
- Policy functions return `None` on unusable model replies, never raise (`curriculum.py` idiom).
- Formats are exactly: `outline | narrative | mnemonic | worked_examples`. Fallback: `outline`.
- Tests: transactional `db_session` / `api_client` fixtures from `tests/conftest.py`; LLM via
  `fake_llm_client(reply)` or `fake_llm_client(script=[FakeTurn(text=…), …])` from
  `app.llm.registry` / `app.llm.providers.fake`; no DB mocking.
- Working branch: `phase-8-notes`. Each task ends with its own commit; stage only your own files
  (never `git add -A`).
- Gate at the end of every task: `uv run poe check` green (backend tasks) / `npm --prefix
  frontend run build && npm --prefix frontend run lint` (frontend tasks).

## File Structure

| File | Responsibility |
|---|---|
| `app/models/note.py` (new) | `Note`, `NoteRevision`, `NoteRender` ORM models |
| `db/migrations/versions/0017_notes.py` (new) | the three tables |
| `app/learning/note_distill.py` (new) | policy layer: atom validation, learner-atom invariant, distill/absorb/render prompts + parsing, mechanical render |
| `app/services/notes.py` (new) | orchestration: staleness, gather, refresh, edit absorb, format cascade, revisions/restore, index; owns transactions |
| `app/schemas/note.py` (new) | Pydantic request/response models |
| `app/api/v1/notes.py` (new) + `app/api/v1/__init__.py` (modify) | endpoints |
| `app/learning/profile_estimators.py` (modify) | `note_format` dimension |
| `app/core/config.py` (modify) | gather caps |
| `frontend/src/api/notes.ts` (new) | types + TanStack hooks (raw fetch, `onboarding.ts` idiom) |
| `frontend/src/pages/Notes.tsx`, `frontend/src/pages/NoteView.tsx`, `frontend/src/components/notes/HistoryDrawer.tsx` (new) | UI |
| `frontend/src/App.tsx`, `frontend/src/components/NavBar.tsx` (modify) | routes + nav |

---

### Task 1: Models + migration `0017_notes`

**Files:**
- Create: `app/models/note.py`
- Modify: `app/models/__init__.py` (add the note imports, mirroring how the other model modules are re-exported there — read the file first and match its exact style)
- Create: `db/migrations/versions/0017_notes.py`
- Test: `tests/test_notes_models.py`

**Interfaces:**
- Consumes: `UUIDPrimaryKeyMixin`, `TimestampMixin` (`app/models/mixins.py`), `Base` (`app/core/db.py`)
- Produces: `Note(learner_id, topic_id, substrate: list, watermark: datetime, format: str | None, revision_ordinal: int)`, `NoteRevision(note_id, ordinal, substrate, cause)`, `NoteRender(note_id, revision_ordinal, format, content_md)`

- [ ] **Step 1: Write the models**

Create `app/models/note.py`:

```python
"""Notes: the durable, per-learner learning artifact (MASTERPLAN §4.9, Phase 8).

``Note.substrate`` is the meta-wiki: a format-neutral JSONB list of KC-tagged *atoms*
(``{"id", "kind": concept|example|callout|learner, "kc_ids", "md", "provenance"}``) — the
source of truth distillation and edit-absorption update. Rendered notes are cached
projections of it (``NoteRender``), regenerated freely per format. Distinct from
``ContentBlock`` (shared across learners) and ``Memory`` (facts about the learner).
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

# Naive-UTC sentinel for "never distilled" — comparisons against the tz-naive
# LearningEvent/Message created_at columns must stay naive (see app/learning/activity.py).
WATERMARK_EPOCH = datetime(1970, 1, 1)


class Note(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One living note per (learner, topic). ``watermark`` = last-distilled activity cutoff."""

    __tablename__ = "notes"
    __table_args__ = (UniqueConstraint("learner_id", "topic_id"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    substrate: Mapped[list] = mapped_column(JSONB, default=list)
    watermark: Mapped[datetime] = mapped_column(default=WATERMARK_EPOCH)
    # Explicit learner format choice (outline|narrative|mnemonic|worked_examples);
    # NULL = auto (cascade: learned note_format dimension -> heuristic -> outline).
    format: Mapped[str | None] = mapped_column(default=None)
    revision_ordinal: Mapped[int] = mapped_column(default=0)


class NoteRevision(UUIDPrimaryKeyMixin, Base):
    """Append-only substrate snapshot per change. History is never rewritten."""

    __tablename__ = "note_revisions"
    __table_args__ = (UniqueConstraint("note_id", "ordinal"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column()
    substrate: Mapped[list] = mapped_column(JSONB, default=list)
    cause: Mapped[str] = mapped_column()  # distill | learner_edit | restore
    created_at: Mapped[datetime] = mapped_column(
        server_default=__import__("sqlalchemy").func.now(), index=True
    )


class NoteRender(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Cached projection of one revision into one format. Only current-revision renders kept."""

    __tablename__ = "note_renders"
    __table_args__ = (UniqueConstraint("note_id", "revision_ordinal", "format"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), index=True
    )
    revision_ordinal: Mapped[int] = mapped_column()
    format: Mapped[str] = mapped_column()
    content_md: Mapped[str] = mapped_column(Text)
```

Note the `NoteRevision.created_at` line above uses an inline `__import__` only to show intent
compactly here — in the actual file, import `func` at the top (`from sqlalchemy import
ForeignKey, Text, UniqueConstraint, func`) and write
`created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)`.

- [ ] **Step 2: Register the models in `app/models/__init__.py`**

Read the file; add imports for `Note`, `NoteRender`, `NoteRevision` from `app.models.note`
exactly mirroring how e.g. the `chat` or `profile` models are imported/re-exported there
(including `__all__` if the file maintains one).

- [ ] **Step 3: Write the migration**

Create `db/migrations/versions/0017_notes.py`:

```python
"""notes

Revision ID: 0017_notes
Revises: 0016_conversation_kind
Create Date: 2026-07-27 00:00:00.000000

Phase 8. ``notes`` (one living note per learner+topic; JSONB atom substrate + watermark),
``note_revisions`` (append-only history), ``note_renders`` (cached per-format projections).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017_notes"
down_revision: str | Sequence[str] | None = "0016_conversation_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("substrate", postgresql.JSONB(), nullable=False),
        sa.Column("watermark", sa.TIMESTAMP(), nullable=False),
        sa.Column("format", sa.String(), nullable=True),
        sa.Column("revision_ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("learner_id", "topic_id"),
    )
    op.create_index("ix_notes_learner_id", "notes", ["learner_id"])
    op.create_index("ix_notes_topic_id", "notes", ["topic_id"])

    op.create_table(
        "note_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("substrate", postgresql.JSONB(), nullable=False),
        sa.Column("cause", sa.String(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", "ordinal"),
    )
    op.create_index("ix_note_revisions_note_id", "note_revisions", ["note_id"])
    op.create_index("ix_note_revisions_created_at", "note_revisions", ["created_at"])

    op.create_table(
        "note_renders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("revision_ordinal", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", "revision_ordinal", "format"),
    )
    op.create_index("ix_note_renders_note_id", "note_renders", ["note_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("note_renders")
    op.drop_table("note_revisions")
    op.drop_table("notes")
```

Before writing, open `db/migrations/versions/0015_conv_sources_citations.py` and confirm the
column-type spellings this repo's hand-written migrations use for UUID PKs and JSONB
(`sa.Uuid()` vs `postgresql.UUID`) — match whatever `0015` uses exactly.

- [ ] **Step 4: Apply + round-trip the migration**

```bash
cd /Users/alykapasi/Desktop/projects/guru-alt
uv run poe db-upgrade && uv run poe db-downgrade && uv run poe db-upgrade
```

Expected: applies, downgrades, re-applies cleanly.

- [ ] **Step 5: Write the model round-trip test**

Create `tests/test_notes_models.py`:

```python
"""Note/NoteRevision/NoteRender: model round-trips + constraints."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject, Topic
from app.models.learner import Learner
from app.models.note import WATERMARK_EPOCH, Note, NoteRender, NoteRevision


async def _learner_topic(db_session: AsyncSession) -> tuple[Learner, Topic]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
    db_session.add(topic)
    await db_session.flush()
    return learner, topic


async def test_note_round_trip_with_revision_and_render(db_session: AsyncSession) -> None:
    learner, topic = await _learner_topic(db_session)
    atoms = [{"id": "a-1", "kind": "concept", "kc_ids": [], "md": "Vectors add.", "provenance": {}}]
    note = Note(learner_id=learner.id, topic_id=topic.id, substrate=atoms, revision_ordinal=1)
    db_session.add(note)
    await db_session.flush()
    assert note.watermark == WATERMARK_EPOCH
    assert note.format is None

    db_session.add(NoteRevision(note_id=note.id, ordinal=1, substrate=atoms, cause="distill"))
    db_session.add(
        NoteRender(note_id=note.id, revision_ordinal=1, format="outline", content_md="- Vectors add.")
    )
    await db_session.flush()

    fetched = await db_session.get(Note, note.id)
    assert fetched is not None
    assert fetched.substrate[0]["md"] == "Vectors add."


async def test_one_note_per_learner_topic(db_session: AsyncSession) -> None:
    learner, topic = await _learner_topic(db_session)
    db_session.add(Note(learner_id=learner.id, topic_id=topic.id))
    await db_session.flush()
    db_session.add(Note(learner_id=learner.id, topic_id=topic.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/test_notes_models.py -v
```

Expected: 2 passed.

- [ ] **Step 7: Full gate + commit**

```bash
uv run poe check
git add app/models/note.py app/models/__init__.py db/migrations/versions/0017_notes.py tests/test_notes_models.py
git commit -m "feat(notes): Note/NoteRevision/NoteRender models + migration 0017

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Policy layer — `app/learning/note_distill.py`

**Files:**
- Create: `app/learning/note_distill.py`
- Test: `tests/test_note_distill.py`

**Interfaces:**
- Consumes: `LLMClient.complete(role, messages, *, system, max_tokens)` → object with `.content`/`.usage`; `ChatMessage`, `ChatRole`, `ModelRole`, `Usage` from `app.llm` / `app.llm.types`
- Produces (Task 3 relies on these exact names):
  - `FORMATS: tuple[str, ...]`, `FALLBACK_FORMAT = "outline"`, `ATOM_KINDS`, `NOTES_ROLE = ModelRole.SMART`
  - `DistillResult(atoms: list[dict] | None, no_change: bool)` (frozen dataclass)
  - `async distill(llm, *, atoms, transcript, outcomes, reading_level) -> tuple[DistillResult | None, Usage]`
  - `async absorb(llm, *, atoms, previous_render, edited_md) -> tuple[list[dict] | None, Usage]`
  - `async render(llm, *, atoms, note_format, reading_level) -> tuple[str, Usage]`
  - `mechanical_render(atoms: list[dict]) -> str`
  - `assign_atom_ids(atoms) -> list[dict]`, `parse_atoms_payload(reply) -> dict | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_note_distill.py`:

```python
"""Notes policy layer: parsing, the learner-atom invariant, distill/absorb/render."""

import json

from app.learning import note_distill
from app.llm.registry import fake_llm_client

CONCEPT = {"id": "a-c1", "kind": "concept", "kc_ids": [], "md": "Dot products measure alignment.", "provenance": {}}
LEARNER_ATOM = {"id": "a-L1", "kind": "learner", "kc_ids": [], "md": "My mnemonic: SOH-CAH-TOA.", "provenance": {}}


def _atoms_reply(atoms: list[dict]) -> str:
    return json.dumps({"atoms": atoms})


class TestParsing:
    def test_valid_payload(self) -> None:
        data = note_distill.parse_atoms_payload(_atoms_reply([CONCEPT]))
        assert data is not None and data["atoms"][0]["md"] == CONCEPT["md"]

    def test_fenced_payload(self) -> None:
        fenced = f"```json\n{_atoms_reply([CONCEPT])}\n```"
        assert note_distill.parse_atoms_payload(fenced) is not None

    def test_no_change_payload(self) -> None:
        data = note_distill.parse_atoms_payload('{"no_change": true}')
        assert data == {"no_change": True}

    def test_garbage_returns_none(self) -> None:
        assert note_distill.parse_atoms_payload("not json at all") is None

    def test_bad_kind_returns_none(self) -> None:
        bad = _atoms_reply([{"kind": "poem", "md": "x"}])
        assert note_distill.parse_atoms_payload(bad) is None

    def test_missing_md_returns_none(self) -> None:
        assert note_distill.parse_atoms_payload(_atoms_reply([{"kind": "concept"}])) is None


class TestAssignIds:
    def test_missing_ids_filled_existing_kept(self) -> None:
        atoms = note_distill.assign_atom_ids([dict(CONCEPT), {"kind": "example", "kc_ids": [], "md": "x"}])
        assert atoms[0]["id"] == "a-c1"
        assert atoms[1]["id"].startswith("a-") and len(atoms[1]["id"]) > 2


class TestDistill:
    async def test_distill_returns_atoms(self) -> None:
        llm = fake_llm_client(_atoms_reply([CONCEPT]))
        result, usage = await note_distill.distill(
            llm, atoms=[], transcript="tutor: dot products…", outcomes="", reading_level=None
        )
        assert result is not None and not result.no_change
        assert result.atoms is not None and result.atoms[0]["md"] == CONCEPT["md"]

    async def test_distill_no_change(self) -> None:
        llm = fake_llm_client('{"no_change": true}')
        result, _ = await note_distill.distill(llm, atoms=[CONCEPT], transcript="t", outcomes="", reading_level=None)
        assert result is not None and result.no_change and result.atoms is None

    async def test_distill_parse_failure_returns_none(self) -> None:
        llm = fake_llm_client("garbage")
        result, _ = await note_distill.distill(llm, atoms=[], transcript="t", outcomes="", reading_level=None)
        assert result is None

    async def test_learner_atom_invariant_rejects_dropping(self) -> None:
        """THE key regression test: a distill that loses a learner atom is rejected."""
        llm = fake_llm_client(_atoms_reply([CONCEPT]))  # reply omits LEARNER_ATOM
        result, _ = await note_distill.distill(
            llm, atoms=[CONCEPT, LEARNER_ATOM], transcript="t", outcomes="", reading_level=None
        )
        assert result is None

    async def test_learner_atom_carried_is_accepted(self) -> None:
        llm = fake_llm_client(_atoms_reply([CONCEPT, LEARNER_ATOM]))
        result, _ = await note_distill.distill(
            llm, atoms=[CONCEPT, LEARNER_ATOM], transcript="t", outcomes="", reading_level=None
        )
        assert result is not None and result.atoms is not None
        assert any(a["id"] == "a-L1" for a in result.atoms)


class TestAbsorb:
    async def test_absorb_returns_atoms_and_may_drop_learner_atoms(self) -> None:
        """Absorb trusts the learner: their edit may delete even their own earlier atoms."""
        llm = fake_llm_client(_atoms_reply([CONCEPT]))
        atoms, _ = await note_distill.absorb(
            llm, atoms=[CONCEPT, LEARNER_ATOM], previous_render="…", edited_md="…"
        )
        assert atoms is not None and len(atoms) == 1

    async def test_absorb_failure_returns_none(self) -> None:
        llm = fake_llm_client("nope")
        atoms, _ = await note_distill.absorb(llm, atoms=[], previous_render="", edited_md="x")
        assert atoms is None


class TestRender:
    async def test_render_returns_markdown(self) -> None:
        llm = fake_llm_client("# Dot products\n\nThey measure alignment.")
        md, usage = await note_distill.render(
            llm, atoms=[CONCEPT], note_format="narrative", reading_level=None
        )
        assert md.startswith("# Dot products")


class TestMechanicalRender:
    def test_groups_by_kind(self) -> None:
        md = note_distill.mechanical_render([CONCEPT, LEARNER_ATOM])
        assert "## Concepts" in md and "## Your notes" in md
        assert md.index("## Concepts") < md.index("## Your notes")
        assert CONCEPT["md"] in md and LEARNER_ATOM["md"] in md

    def test_empty_substrate(self) -> None:
        assert note_distill.mechanical_render([]) == ""
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_note_distill.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.learning.note_distill'`.

- [ ] **Step 3: Implement the module**

Create `app/learning/note_distill.py`:

```python
"""Notes policy layer: distillation, edit absorption, rendering (MASTERPLAN §4.9, Phase 8).

The substrate/projection split: a format-neutral list of KC-tagged *atoms* is the source of
truth; rendered notes are projections of it into a named format. Mirrors ``curriculum.py``'s
tolerant-parse idiom — policy functions return ``None`` on an unusable reply, never raise.

The one hard guarantee lives here: a **distill** merge that drops a learner-contributed atom
is rejected in code (``_learner_atoms_preserved``), not merely prompted against. **Absorb**
deliberately skips that check — the learner's own edit is the authority over their content.
"""

import json
import uuid
from dataclasses import dataclass

import structlog

from app.llm import ChatMessage, ChatRole, LLMClient
from app.llm.types import ModelRole, Usage

log = structlog.get_logger(__name__)

NOTES_ROLE = ModelRole.SMART
FORMATS = ("outline", "narrative", "mnemonic", "worked_examples")
FALLBACK_FORMAT = "outline"
ATOM_KINDS = ("concept", "example", "callout", "learner")

_KIND_HEADINGS = {
    "concept": "## Concepts",
    "example": "## Worked examples",
    "callout": "## Watch out",
    "learner": "## Your notes",
}

DISTILL_SYSTEM_PROMPT = (
    "You maintain a learner's personal study notes as a list of atoms — small, self-contained "
    "markdown units. Kinds: 'concept' (an explanation of covered material), 'example' (a worked "
    "example, ideally one the learner actually worked through), 'callout' (a warning about "
    "something THIS learner got wrong, naming the actual confusion), 'learner' (content the "
    "learner wrote themselves — carry every one of these forward, keeping its id; you may "
    "lightly edit its wording for flow but never drop one or change its meaning). "
    "Fold the new material and outcomes into the existing atoms: restructure freely, merge "
    "duplicates, keep ids for atoms you carry forward, omit ids for genuinely new atoms. "
    'Reply with JSON only: {"atoms": [{"id": "...", "kind": "...", "kc_ids": [], "md": "...", '
    '"provenance": {}}]} — or {"no_change": true} if the new activity contains nothing '
    "relevant to this topic."
)

ABSORB_SYSTEM_PROMPT = (
    "The learner edited the rendered version of their study notes. Update the underlying atom "
    "list to match their intent: content they added becomes new 'learner' atoms; content they "
    "changed updates the matching atom; content they removed is deleted (their edit is the "
    "authority, even over their own earlier notes). Keep ids for atoms you carry forward. "
    'Reply with JSON only: {"atoms": [...]} in the same atom schema you were given.'
)

RENDER_SYSTEM_PROMPT = (
    "You write a learner's personal study notes as one coherent markdown document from the "
    "atom list given. The atoms are your source material — order and group them as the format "
    "demands; do not invent facts that are not in the atoms; keep every 'callout' visible. "
    "Write an actual document, not a list of fragments."
)

_FORMAT_INSTRUCTIONS = {
    "outline": "Format: dense structured outline — nested bullets, bold key terms, minimal prose.",
    "narrative": "Format: flowing narrative prose with short headed sections.",
    "mnemonic": "Format: memory-hook heavy — lead each section with a mnemonic, acronym, or vivid anchor.",
    "worked_examples": "Format: example-led — each section opens with a worked example, then the principle.",
}


@dataclass(frozen=True)
class DistillResult:
    """``atoms`` is None iff ``no_change``."""

    atoms: list[dict] | None
    no_change: bool


def _extract_json(content: str) -> str:
    """First '{' to last '}' — tolerates markdown fences. Raises ValueError if absent."""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]


def parse_atoms_payload(reply: str) -> dict | None:
    """Parse a distill/absorb reply into ``{"no_change": True}`` or ``{"atoms": [...]}``.

    Shape-validates every atom (kind, non-empty md, list kc_ids). Returns None on anything
    unusable — the caller keeps the old substrate.
    """
    try:
        data = json.loads(_extract_json(reply))
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("note_distill.parse_failed", error=str(exc))
        return None
    if not isinstance(data, dict):
        return None
    if data.get("no_change") is True:
        return {"no_change": True}
    atoms = data.get("atoms")
    if not isinstance(atoms, list):
        log.warning("note_distill.parse_failed", reason="atoms_not_list")
        return None
    cleaned: list[dict] = []
    for atom in atoms:
        if not isinstance(atom, dict):
            return None
        if atom.get("kind") not in ATOM_KINDS:
            log.warning("note_distill.parse_failed", reason="bad_kind", kind=atom.get("kind"))
            return None
        md = atom.get("md")
        if not isinstance(md, str) or not md.strip():
            log.warning("note_distill.parse_failed", reason="missing_md")
            return None
        kc_ids = atom.get("kc_ids")
        cleaned.append(
            {
                "id": atom.get("id") if isinstance(atom.get("id"), str) else None,
                "kind": atom["kind"],
                "kc_ids": [str(k) for k in kc_ids] if isinstance(kc_ids, list) else [],
                "md": md.strip(),
                "provenance": atom.get("provenance") if isinstance(atom.get("provenance"), dict) else {},
            }
        )
    return {"atoms": cleaned}


def assign_atom_ids(atoms: list[dict]) -> list[dict]:
    """Fill missing/None ids with fresh short ids; existing ids are kept untouched."""
    for atom in atoms:
        if not atom.get("id"):
            atom["id"] = f"a-{uuid.uuid4().hex[:6]}"
    return atoms


def _learner_atoms_preserved(prior: list[dict], new: list[dict]) -> bool:
    """Every learner-kind atom id in ``prior`` must appear in ``new`` (distill only)."""
    prior_ids = {a["id"] for a in prior if a.get("kind") == "learner" and a.get("id")}
    new_ids = {a.get("id") for a in new}
    return prior_ids <= new_ids


def _profile_line(reading_level: object) -> str:
    if reading_level is None:
        return ""
    return f"\n\nWrite at roughly this reading level: {reading_level}."


async def distill(
    llm: LLMClient,
    *,
    atoms: list[dict],
    transcript: str,
    outcomes: str,
    reading_level: object,
) -> tuple[DistillResult | None, Usage]:
    """One merge: current atoms + new material + outcomes -> updated atom list.

    Returns (None, usage) on parse failure or a learner-atom-invariant violation.
    """
    prompt = (
        f"Current atoms:\n{json.dumps(atoms, indent=2)}\n\n"
        f"New study activity (transcript excerpts):\n{transcript or '(none)'}\n\n"
        f"The learner's graded outcomes on this topic (build 'callout' atoms from real "
        f"mistakes here):\n{outcomes or '(none)'}"
        f"{_profile_line(reading_level)}"
    )
    completion = await llm.complete(
        NOTES_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=DISTILL_SYSTEM_PROMPT,
        max_tokens=4096,
    )
    data = parse_atoms_payload(completion.content)
    if data is None:
        return None, completion.usage
    if data.get("no_change"):
        return DistillResult(atoms=None, no_change=True), completion.usage
    new_atoms = data["atoms"]
    if not _learner_atoms_preserved(atoms, new_atoms):
        log.warning("note_distill.learner_atom_dropped")
        return None, completion.usage
    return DistillResult(atoms=assign_atom_ids(new_atoms), no_change=False), completion.usage


async def absorb(
    llm: LLMClient,
    *,
    atoms: list[dict],
    previous_render: str,
    edited_md: str,
) -> tuple[list[dict] | None, Usage]:
    """Fold a learner's edit of the rendered note back into the substrate."""
    prompt = (
        f"Current atoms:\n{json.dumps(atoms, indent=2)}\n\n"
        f"The rendered note they started from:\n{previous_render}\n\n"
        f"Their edited version:\n{edited_md}"
    )
    completion = await llm.complete(
        NOTES_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=ABSORB_SYSTEM_PROMPT,
        max_tokens=4096,
    )
    data = parse_atoms_payload(completion.content)
    if data is None or data.get("no_change"):
        return None, completion.usage
    return assign_atom_ids(data["atoms"]), completion.usage


async def render(
    llm: LLMClient,
    *,
    atoms: list[dict],
    note_format: str,
    reading_level: object,
) -> tuple[str, Usage]:
    """Project the substrate into one format. Free-text markdown; no parsing to fail."""
    prompt = (
        f"{_FORMAT_INSTRUCTIONS[note_format]}{_profile_line(reading_level)}\n\n"
        f"Atoms:\n{json.dumps(atoms, indent=2)}"
    )
    completion = await llm.complete(
        NOTES_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=RENDER_SYSTEM_PROMPT,
        max_tokens=4096,
    )
    return completion.content.strip(), completion.usage


def mechanical_render(atoms: list[dict]) -> str:
    """Deterministic, LLM-free markdown of a substrate — the revision source view."""
    sections: list[str] = []
    for kind in ATOM_KINDS:
        group = [a["md"] for a in atoms if a.get("kind") == kind]
        if group:
            sections.append("\n\n".join([_KIND_HEADINGS[kind], *group]))
    return "\n\n".join(sections)
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_note_distill.py -v
```

Expected: all pass (16 tests).

- [ ] **Step 5: Full gate + commit**

```bash
uv run poe check
git add app/learning/note_distill.py tests/test_note_distill.py
git commit -m "feat(notes): distill/absorb/render policy layer with learner-atom invariant

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Orchestration — `app/services/notes.py` + config

**Files:**
- Modify: `app/core/config.py` (append inside `Settings`, before the `runtime_typecheck` property)
- Create: `app/services/notes.py`
- Test: `tests/test_notes_service.py`

**Interfaces:**
- Consumes: everything Task 2 produces; `Note`/`NoteRevision`/`NoteRender` (Task 1);
  `log_llm_call` (`app.services.llm_log`); `ProfileDimension` (`app.models.profile`);
  `LearningEvent` (`app.models.learning`); `Message`, `Conversation` (`app.models.chat`);
  `KC`, `Topic` (`app.models.knowledge`); `Item` (`app.models.assessment`)
- Produces (Task 4 relies on these exact names):
  - `NoteView(topic_id, content_md: str | None, format: str | None, effective_format: str, stale: bool, revision_ordinal: int | None, updated_at: datetime | None)` (frozen dataclass)
  - `async note_view(session, learner_id, topic) -> NoteView`
  - `async refresh_note(session, llm, learner_id, topic) -> NoteView`
  - `async absorb_edit(session, llm, learner_id, topic, content_md) -> NoteView | None` (None = nothing to edit or absorb failed — caller distinguishes via `get_note`)
  - `async set_format(session, llm, learner_id, topic, note_format: str | None) -> NoteView`
  - `async get_note(session, learner_id, topic_id) -> Note | None`
  - `async list_revisions(session, learner_id, topic) -> list[NoteRevision]`
  - `async revision_source(session, learner_id, topic, ordinal) -> str | None`
  - `async restore_revision(session, llm, learner_id, topic, ordinal) -> NoteView | None`
  - `async notes_index(session, learner_id, subject_id) -> list[dict]` (each: `{topic_id, topic_name, has_note, stale, updated_at}`)

- [ ] **Step 1: Add the config caps**

In `app/core/config.py`, append to `Settings` (before the `runtime_typecheck` property),
matching the commented style of the neighboring fields:

```python
    # Notes (Phase 8). Caps on what one catch-up distillation reads — cost/UX bounds, same
    # idiom as memory_extraction_window. Messages come from subject-scoped conversations
    # past the note's watermark; outcome events from the topic's KCs.
    note_distill_max_messages: int = 150
    note_distill_max_outcome_events: int = 50
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_notes_service.py`:

```python
"""Notes orchestration: staleness, catch-up refresh, absorb, restore, format cascade."""

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import note_distill
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.note import Note, NoteRevision
from app.models.profile import ProfileDimension
from app.services import notes as notes_svc

ATOMS = [{"id": "a-1", "kind": "concept", "kc_ids": [], "md": "Vectors add tip-to-tail.", "provenance": {}}]
LEARNER_ATOM = {"id": "a-L1", "kind": "learner", "kc_ids": [], "md": "My own mnemonic.", "provenance": {}}
RENDERED = "# Vectors\n\nThey add tip-to-tail."


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _distill_then_render(atoms: list[dict]) -> object:
    """Fake LLM scripted for one refresh: distill reply, then render reply."""
    return fake_llm_client(
        script=[FakeTurn(text=json.dumps({"atoms": atoms})), FakeTurn(text=RENDERED)]
    )


async def _seed(db_session: AsyncSession) -> tuple[Learner, Topic, KC]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="Vectors")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Vector addition")
    db_session.add(kc)
    await db_session.flush()
    return learner, topic, kc


async def _add_observation(db_session: AsyncSession, learner: Learner, kc: KC) -> None:
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=kc.id,
            event_type="observation",
            payload={"score": 0.0, "item_id": None, "response": {"text": "wrong"}, "hints_used": 1},
        )
    )
    await db_session.flush()


async def _add_message(db_session: AsyncSession, learner: Learner, topic: Topic) -> None:
    conv = Conversation(learner_id=learner.id, subject_id=topic.subject_id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add(Message(conversation_id=conv.id, role="assistant", content="Vectors add tip-to-tail."))
    await db_session.flush()


async def test_first_refresh_creates_note_revision_and_render(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    view = await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    assert view.content_md == RENDERED
    assert view.revision_ordinal == 1
    assert view.stale is False
    note = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert note is not None and note.watermark > notes_svc.EPOCH


async def test_message_activity_marks_stale(db_session: AsyncSession) -> None:
    learner, topic, _kc = await _seed(db_session)
    await _add_message(db_session, learner, topic)
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.stale is True and view.content_md is None


async def test_no_activity_is_not_stale_and_refresh_is_noop(db_session: AsyncSession) -> None:
    learner, topic, _kc = await _seed(db_session)
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.stale is False
    # refresh with no activity must not call the LLM at all: a script would raise if consumed
    view = await notes_svc.refresh_note(db_session, fake_llm_client(script=[]), learner.id, topic)
    assert view.content_md is None and view.revision_ordinal is None


async def test_no_change_advances_watermark_without_revision(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    llm = fake_llm_client('{"no_change": true}')
    view = await notes_svc.refresh_note(db_session, llm, learner.id, topic)
    assert view.stale is False  # watermark advanced past the event
    note = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert note is not None and note.revision_ordinal == 0 and note.watermark > notes_svc.EPOCH


async def test_distill_failure_keeps_substrate_and_watermark(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    view = await notes_svc.refresh_note(db_session, fake_llm_client("garbage"), learner.id, topic)
    assert view.stale is True  # nothing advanced; retried on next refresh
    assert await notes_svc.get_note(db_session, learner.id, topic.id) is None


async def test_learner_atom_survives_or_merge_rejected(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    note = Note(
        learner_id=learner.id, topic_id=topic.id,
        substrate=[*ATOMS, LEARNER_ATOM], revision_ordinal=1, watermark=notes_svc.EPOCH,
    )
    db_session.add(note)
    db_session.add(NoteRevision(note_id=note.id, ordinal=1, substrate=note.substrate, cause="distill"))
    await db_session.flush()
    await _add_observation(db_session, learner, kc)
    # distill reply drops the learner atom -> merge rejected, substrate intact
    view = await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    assert view.stale is True
    refreshed = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert refreshed is not None
    assert any(a["id"] == "a-L1" for a in refreshed.substrate)


async def test_absorb_edit_creates_learner_edit_revision(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    edited_atoms = [*ATOMS, LEARNER_ATOM]
    llm = fake_llm_client(script=[FakeTurn(text=json.dumps({"atoms": edited_atoms})), FakeTurn(text=RENDERED)])
    view = await notes_svc.absorb_edit(db_session, llm, learner.id, topic, "my edited note")
    assert view is not None and view.revision_ordinal == 2
    revs = await notes_svc.list_revisions(db_session, learner.id, topic)
    assert [r.cause for r in revs] == ["distill", "learner_edit"]


async def test_restore_copies_forward_as_new_revision(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    llm = fake_llm_client(script=[FakeTurn(text=json.dumps({"atoms": [*ATOMS, LEARNER_ATOM]})), FakeTurn(text=RENDERED)])
    await notes_svc.absorb_edit(db_session, llm, learner.id, topic, "edit")
    view = await notes_svc.restore_revision(
        db_session, fake_llm_client(script=[FakeTurn(text=RENDERED)]), learner.id, topic, 1
    )
    assert view is not None and view.revision_ordinal == 3
    note = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert note is not None and not any(a["kind"] == "learner" for a in note.substrate)


async def test_revision_source_is_mechanical(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    src = await notes_svc.revision_source(db_session, learner.id, topic, 1)
    assert src is not None and "## Concepts" in src


async def test_format_cascade(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    # fallback
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.effective_format == note_distill.FALLBACK_FORMAT
    # learned dimension wins over fallback
    db_session.add(ProfileDimension(
        learner_id=learner.id, key="note_format", value="narrative",
        uncertainty=0.2, kind="trait", source="behavioral",
    ))
    await db_session.flush()
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.effective_format == "narrative"
    # explicit choice wins over everything; set_format also renders the new format
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    llm = fake_llm_client(script=[FakeTurn(text="mnemonic render")])
    view = await notes_svc.set_format(db_session, llm, learner.id, topic, "mnemonic")
    assert view.effective_format == "mnemonic" and view.content_md == "mnemonic render"


async def test_conceptual_error_share_biases_worked_examples(db_session: AsyncSession) -> None:
    learner, topic, _kc = await _seed(db_session)
    db_session.add(ProfileDimension(
        learner_id=learner.id, key="error_type",
        value={"conceptual": 0.6, "procedural": 0.3, "careless": 0.1},
        uncertainty=0.3, kind="trait", source="behavioral",
    ))
    await db_session.flush()
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.effective_format == "worked_examples"


async def test_notes_index(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    entries = await notes_svc.notes_index(db_session, learner.id, topic.subject_id)
    assert len(entries) == 1
    assert entries[0]["topic_id"] == topic.id
    assert entries[0]["has_note"] is False and entries[0]["stale"] is True
```

- [ ] **Step 3: Run to verify failure**

```bash
uv run pytest tests/test_notes_service.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.notes'`.

- [ ] **Step 4: Implement the service**

Create `app/services/notes.py`:

```python
"""Notes orchestration: staleness, catch-up distillation, edits, history (Phase 8).

Owns transactions and cost logging; all model-reply handling lives in
``app/learning/note_distill.py``. GET-path functions (``note_view``, ``notes_index``) are
pure reads — the work happens behind explicit refresh/edit calls, avoiding the
"GET with generation side-effects" compromise the reviews-due endpoint had to accept.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import note_distill
from app.learning.note_distill import FALLBACK_FORMAT, FORMATS, NOTES_ROLE
from app.llm import LLMClient
from app.models.assessment import Item
from app.models.chat import Conversation, Message
from app.models.knowledge import KC, Topic
from app.models.learning import LearningEvent
from app.models.note import WATERMARK_EPOCH, Note, NoteRender, NoteRevision
from app.models.profile import ProfileDimension
from app.services.llm_log import log_llm_call

log = structlog.get_logger(__name__)

EPOCH = WATERMARK_EPOCH


@dataclass(frozen=True)
class NoteView:
    """What the API returns for a note in any state (including 'no note yet')."""

    topic_id: uuid.UUID
    content_md: str | None
    format: str | None
    effective_format: str
    stale: bool
    revision_ordinal: int | None
    updated_at: datetime | None


def _now() -> datetime:
    # Naive UTC on purpose: LearningEvent/Message.created_at are tz-naive columns; a tz-aware
    # comparison crashes asyncpg (see app/learning/activity.py). Do not "fix" to tz-aware.
    return datetime.now(UTC).replace(tzinfo=None)


async def get_note(session: AsyncSession, learner_id: uuid.UUID, topic_id: uuid.UUID) -> Note | None:
    return await session.scalar(
        select(Note).where(Note.learner_id == learner_id, Note.topic_id == topic_id)
    )


async def _dimension_value(session: AsyncSession, learner_id: uuid.UUID, key: str) -> object:
    dim = await session.scalar(
        select(ProfileDimension).where(
            ProfileDimension.learner_id == learner_id, ProfileDimension.key == key
        )
    )
    return dim.value if dim is not None else None


async def effective_format(session: AsyncSession, learner_id: uuid.UUID, note: Note | None) -> str:
    """The cascade: explicit choice > learned note_format dimension > heuristic > outline."""
    if note is not None and note.format:
        return note.format
    learned = await _dimension_value(session, learner_id, "note_format")
    if isinstance(learned, str) and learned in FORMATS:
        return learned
    # Heuristic: a majority-conceptual error profile benefits from example-led notes.
    errors = await _dimension_value(session, learner_id, "error_type")
    if isinstance(errors, dict) and errors.get("conceptual", 0) >= 0.5:
        return "worked_examples"
    return FALLBACK_FORMAT


async def _reading_level(session: AsyncSession, learner_id: uuid.UUID) -> object:
    return await _dimension_value(session, learner_id, "reading_level")


async def _has_new_activity(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, watermark: datetime
) -> bool:
    kc_ids = select(KC.id).where(KC.topic_id == topic.id).scalar_subquery()
    event = await session.scalar(
        select(LearningEvent.id)
        .where(
            LearningEvent.learner_id == learner_id,
            LearningEvent.kc_id.in_(kc_ids),
            LearningEvent.created_at > watermark,
        )
        .limit(1)
    )
    if event is not None:
        return True
    message = await session.scalar(
        select(Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.learner_id == learner_id,
            Conversation.subject_id == topic.subject_id,
            Message.created_at > watermark,
        )
        .limit(1)
    )
    return message is not None


async def _current_render(session: AsyncSession, note: Note, note_format: str) -> NoteRender | None:
    return await session.scalar(
        select(NoteRender).where(
            NoteRender.note_id == note.id,
            NoteRender.revision_ordinal == note.revision_ordinal,
            NoteRender.format == note_format,
        )
    )


async def _is_stale(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, note: Note | None, fmt: str
) -> bool:
    watermark = note.watermark if note is not None else EPOCH
    if await _has_new_activity(session, learner_id, topic, watermark):
        return True
    # Render-failure recovery: substrate current but no cached render for the effective format.
    if note is not None and note.revision_ordinal > 0:
        return await _current_render(session, note, fmt) is None
    return False


async def _view(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, note: Note | None
) -> NoteView:
    fmt = await effective_format(session, learner_id, note)
    content = None
    if note is not None and note.revision_ordinal > 0:
        render_row = await _current_render(session, note, fmt)
        content = render_row.content_md if render_row is not None else None
    return NoteView(
        topic_id=topic.id,
        content_md=content,
        format=note.format if note is not None else None,
        effective_format=fmt,
        stale=await _is_stale(session, learner_id, topic, note, fmt),
        revision_ordinal=(
            note.revision_ordinal if note is not None and note.revision_ordinal > 0 else None
        ),
        updated_at=note.updated_at if note is not None else None,
    )


async def note_view(session: AsyncSession, learner_id: uuid.UUID, topic: Topic) -> NoteView:
    """Pure read — never calls a model, never writes."""
    return await _view(session, learner_id, topic, await get_note(session, learner_id, topic.id))


@dataclass(frozen=True)
class _Gathered:
    transcript: str
    outcomes: str
    latest: datetime | None


async def _gather(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, watermark: datetime
) -> _Gathered:
    settings = get_settings()
    kcs = (await session.scalars(select(KC).where(KC.topic_id == topic.id))).all()
    kc_names = {kc.id: kc.name for kc in kcs}

    events = (
        await session.scalars(
            select(LearningEvent)
            .where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.kc_id.in_(list(kc_names)),
                LearningEvent.created_at > watermark,
                LearningEvent.event_type == "observation",
            )
            .order_by(LearningEvent.created_at)
            .limit(settings.note_distill_max_outcome_events)
        )
    ).all()

    item_ids = {
        uuid.UUID(e.payload["item_id"]) for e in events if e.payload.get("item_id")
    }
    items: dict[uuid.UUID, Item] = {}
    if item_ids:
        rows = (await session.scalars(select(Item).where(Item.id.in_(item_ids)))).all()
        items = {item.id: item for item in rows}

    outcome_lines: list[str] = []
    for event in events:
        kc_name = kc_names.get(event.kc_id, "?")
        line = f"- KC '{kc_name}': score={event.payload.get('score')}, hints={event.payload.get('hints_used', 0)}"
        item_id = event.payload.get("item_id")
        if item_id and uuid.UUID(item_id) in items:
            line += f"; question: {items[uuid.UUID(item_id)].stem!r}"
        if event.payload.get("response") is not None:
            line += f"; their answer: {event.payload['response']!r}"
        outcome_lines.append(line)

    messages = (
        await session.scalars(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.learner_id == learner_id,
                Conversation.subject_id == topic.subject_id,
                Message.created_at > watermark,
            )
            .order_by(Message.created_at)
            .limit(settings.note_distill_max_messages)
        )
    ).all()
    transcript_lines = [f"{m.role}: {m.content}" for m in messages]

    timestamps = [e.created_at for e in events] + [m.created_at for m in messages]
    return _Gathered(
        transcript="\n".join(transcript_lines),
        outcomes="\n".join(outcome_lines),
        latest=max(timestamps) if timestamps else None,
    )


async def _render_and_cache(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, note: Note, fmt: str
) -> NoteRender:
    content, usage = await note_distill.render(
        llm,
        atoms=note.substrate,
        note_format=fmt,
        reading_level=await _reading_level(session, learner_id),
    )
    await log_llm_call(
        session, learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )
    render_row = NoteRender(
        note_id=note.id, revision_ordinal=note.revision_ordinal, format=fmt, content_md=content
    )
    session.add(render_row)
    await session.flush()
    return render_row


async def _commit_new_revision(
    session: AsyncSession, note: Note, atoms: list[dict], cause: str
) -> None:
    """Advance the note to a new substrate revision; drops all cached renders."""
    note.substrate = atoms
    note.revision_ordinal += 1
    session.add(
        NoteRevision(note_id=note.id, ordinal=note.revision_ordinal, substrate=atoms, cause=cause)
    )
    await session.execute(delete(NoteRender).where(NoteRender.note_id == note.id))
    await session.flush()


async def refresh_note(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic
) -> NoteView:
    """The catch-up: distill anything past the watermark, then ensure a render exists."""
    note = await get_note(session, learner_id, topic.id)
    fmt = await effective_format(session, learner_id, note)
    watermark = note.watermark if note is not None else EPOCH

    if not await _has_new_activity(session, learner_id, topic, watermark):
        # Render-only heal (render missing for a current substrate), or nothing to do.
        if note is not None and note.revision_ordinal > 0:
            if await _current_render(session, note, fmt) is None:
                await _render_and_cache(session, llm, learner_id, note, fmt)
                await session.commit()
        return await _view(session, learner_id, topic, note)

    gathered = await _gather(session, learner_id, topic, watermark)
    atoms = note.substrate if note is not None else []
    result, usage = await note_distill.distill(
        llm,
        atoms=atoms,
        transcript=gathered.transcript,
        outcomes=gathered.outcomes,
        reading_level=await _reading_level(session, learner_id),
    )
    await log_llm_call(
        session, learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )

    if result is None:
        # Parse failure or learner-atom violation: keep everything, stay stale, retry later.
        await session.commit()  # persist the cost log
        return await _view(session, learner_id, topic, note)

    new_watermark = gathered.latest or _now()
    if note is None:
        note = Note(learner_id=learner_id, topic_id=topic.id, substrate=[], watermark=new_watermark)
        session.add(note)
        await session.flush()
    else:
        note.watermark = new_watermark

    if result.no_change:
        await session.commit()
        return await _view(session, learner_id, topic, note)

    assert result.atoms is not None
    await _commit_new_revision(session, note, result.atoms, "distill")
    await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    return await _view(session, learner_id, topic, note)


async def absorb_edit(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic, content_md: str
) -> NoteView | None:
    """Fold a learner's edit into the substrate. None = no note yet, or absorb failed."""
    note = await get_note(session, learner_id, topic.id)
    if note is None or note.revision_ordinal == 0:
        return None
    fmt = await effective_format(session, learner_id, note)
    render_row = await _current_render(session, note, fmt)
    previous = render_row.content_md if render_row else note_distill.mechanical_render(note.substrate)
    atoms, usage = await note_distill.absorb(
        llm, atoms=note.substrate, previous_render=previous, edited_md=content_md
    )
    await log_llm_call(
        session, learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )
    if atoms is None:
        await session.commit()  # persist the cost log; note untouched
        return None
    await _commit_new_revision(session, note, atoms, "learner_edit")
    await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    return await _view(session, learner_id, topic, note)


async def set_format(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic, note_format: str | None
) -> NoteView:
    """Set (or clear, None=auto) the explicit format; render the new format if missing."""
    note = await get_note(session, learner_id, topic.id)
    if note is None:
        note = Note(learner_id=learner_id, topic_id=topic.id, substrate=[], watermark=EPOCH)
        session.add(note)
        await session.flush()
    note.format = note_format
    fmt = await effective_format(session, learner_id, note)
    if note.revision_ordinal > 0 and await _current_render(session, note, fmt) is None:
        await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    return await _view(session, learner_id, topic, note)


async def list_revisions(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic
) -> list[NoteRevision]:
    note = await get_note(session, learner_id, topic.id)
    if note is None:
        return []
    rows = await session.scalars(
        select(NoteRevision).where(NoteRevision.note_id == note.id).order_by(NoteRevision.ordinal)
    )
    return list(rows.all())


async def _revision(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, ordinal: int
) -> NoteRevision | None:
    note = await get_note(session, learner_id, topic.id)
    if note is None:
        return None
    return await session.scalar(
        select(NoteRevision).where(NoteRevision.note_id == note.id, NoteRevision.ordinal == ordinal)
    )


async def revision_source(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, ordinal: int
) -> str | None:
    revision = await _revision(session, learner_id, topic, ordinal)
    return note_distill.mechanical_render(revision.substrate) if revision is not None else None


async def restore_revision(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic, ordinal: int
) -> NoteView | None:
    """Copy an old revision's substrate forward as a NEW revision — history is never rewritten."""
    revision = await _revision(session, learner_id, topic, ordinal)
    if revision is None:
        return None
    note = await get_note(session, learner_id, topic.id)
    assert note is not None  # _revision resolved through it
    await _commit_new_revision(session, note, revision.substrate, "restore")
    fmt = await effective_format(session, learner_id, note)
    await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    return await _view(session, learner_id, topic, note)


async def notes_index(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> list[dict]:
    topics = (
        await session.scalars(
            select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.name)
        )
    ).all()
    entries: list[dict] = []
    for topic in topics:
        note = await get_note(session, learner_id, topic.id)
        fmt = await effective_format(session, learner_id, note)
        entries.append(
            {
                "topic_id": topic.id,
                "topic_name": topic.name,
                "has_note": note is not None and note.revision_ordinal > 0,
                "stale": await _is_stale(session, learner_id, topic, note, fmt),
                "updated_at": note.updated_at if note is not None else None,
            }
        )
    return entries
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_notes_service.py -v
```

Expected: all pass (12 tests). If `FakeTurn`'s scripted playback errors on exhaustion, check
`app/llm/providers/fake.py` for its exhaustion behavior and adjust scripts (never the service)
accordingly.

- [ ] **Step 6: Full gate + commit**

```bash
uv run poe check
git add app/services/notes.py app/core/config.py tests/test_notes_service.py
git commit -m "feat(notes): catch-up orchestration service (staleness, refresh, absorb, restore)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Schemas + API endpoints

**Files:**
- Create: `app/schemas/note.py`
- Create: `app/api/v1/notes.py`
- Modify: `app/api/v1/__init__.py`
- Test: `tests/test_notes_api.py`

**Interfaces:**
- Consumes: every Task 3 service function; `SessionDep`, `CurrentLearner`, `LLMClientDep`
  (`app.api.deps`); `knowledge` service `get_topic(session, topic_id)`
- Produces: the eight endpoints of spec §8

- [ ] **Step 1: Write the schemas**

Create `app/schemas/note.py`:

```python
"""Request/response schemas for notes."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NoteFormat = Literal["outline", "narrative", "mnemonic", "worked_examples"]


class NoteRead(BaseModel):
    topic_id: uuid.UUID
    content_md: str | None
    format: NoteFormat | None
    effective_format: NoteFormat
    stale: bool
    revision_ordinal: int | None
    updated_at: datetime | None


class NoteIndexEntry(BaseModel):
    topic_id: uuid.UUID
    topic_name: str
    has_note: bool
    stale: bool
    updated_at: datetime | None


class NoteEditRequest(BaseModel):
    content_md: str = Field(min_length=1)


class NoteFormatRequest(BaseModel):
    format: NoteFormat | None  # None = back to auto


class NoteRevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    cause: str
    created_at: datetime


class NoteRevisionSource(BaseModel):
    ordinal: int
    content_md: str
```

- [ ] **Step 2: Write the endpoints**

Create `app/api/v1/notes.py`:

```python
"""Notes endpoints: living per-topic study notes (Phase 8).

GET is pure; the work (distill/render/absorb — seconds of latency) sits behind explicit
POST/PUT/PATCH so reads never have generation side-effects.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.models.knowledge import Topic
from app.schemas.note import (
    NoteEditRequest,
    NoteFormatRequest,
    NoteIndexEntry,
    NoteRead,
    NoteRevisionRead,
    NoteRevisionSource,
)
from app.services import knowledge as knowledge_svc
from app.services import notes as notes_svc

router = APIRouter(tags=["notes"])


async def _topic_404(session: SessionDep, topic_id: uuid.UUID) -> Topic:
    topic = await knowledge_svc.get_topic(session, topic_id)
    if topic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="topic not found")
    return topic


def _read(view: notes_svc.NoteView) -> NoteRead:
    return NoteRead(
        topic_id=view.topic_id,
        content_md=view.content_md,
        format=view.format,  # type: ignore[arg-type]
        effective_format=view.effective_format,  # type: ignore[arg-type]
        stale=view.stale,
        revision_ordinal=view.revision_ordinal,
        updated_at=view.updated_at,
    )


@router.get("/subjects/{subject_id}/notes", response_model=list[NoteIndexEntry])
async def notes_index(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    subject = await knowledge_svc.get_subject(session, subject_id)
    if subject is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="subject not found")
    return await notes_svc.notes_index(session, learner.id, subject_id)


@router.get("/topics/{topic_id}/note", response_model=NoteRead)
async def get_note(topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    topic = await _topic_404(session, topic_id)
    return _read(await notes_svc.note_view(session, learner.id, topic))


@router.post("/topics/{topic_id}/note/refresh", response_model=NoteRead)
async def refresh_note(
    topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner, llm: LLMClientDep
):
    topic = await _topic_404(session, topic_id)
    return _read(await notes_svc.refresh_note(session, llm, learner.id, topic))


@router.put("/topics/{topic_id}/note", response_model=NoteRead)
async def edit_note(
    topic_id: uuid.UUID,
    request: NoteEditRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    topic = await _topic_404(session, topic_id)
    if await notes_svc.get_note(session, learner.id, topic_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no note to edit yet")
    view = await notes_svc.absorb_edit(session, llm, learner.id, topic, request.content_md)
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Edit could not be absorbed. Your note is unchanged — please try again.",
        )
    return _read(view)


@router.patch("/topics/{topic_id}/note/format", response_model=NoteRead)
async def set_format(
    topic_id: uuid.UUID,
    request: NoteFormatRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    topic = await _topic_404(session, topic_id)
    return _read(await notes_svc.set_format(session, llm, learner.id, topic, request.format))


@router.get("/topics/{topic_id}/note/revisions", response_model=list[NoteRevisionRead])
async def list_revisions(topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    topic = await _topic_404(session, topic_id)
    return await notes_svc.list_revisions(session, learner.id, topic)


@router.get("/topics/{topic_id}/note/revisions/{ordinal}", response_model=NoteRevisionSource)
async def revision_source(
    topic_id: uuid.UUID, ordinal: int, session: SessionDep, learner: CurrentLearner
):
    topic = await _topic_404(session, topic_id)
    source = await notes_svc.revision_source(session, learner.id, topic, ordinal)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="revision not found")
    return NoteRevisionSource(ordinal=ordinal, content_md=source)


@router.post("/topics/{topic_id}/note/revisions/{ordinal}/restore", response_model=NoteRead)
async def restore_revision(
    topic_id: uuid.UUID,
    ordinal: int,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    topic = await _topic_404(session, topic_id)
    view = await notes_svc.restore_revision(session, llm, learner.id, topic, ordinal)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="revision not found")
    return _read(view)
```

- [ ] **Step 3: Register the router**

In `app/api/v1/__init__.py`: add `notes` to the `from app.api.v1 import (…)` list
(alphabetical position: after `memory`, before `onboarding`) and add
`api_router.include_router(notes.router)` alongside the other `include_router` lines.

- [ ] **Step 4: Write the API tests**

Create `tests/test_notes_api.py`:

```python
"""Notes API: endpoint mechanics over the transactional test app."""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE, get_llm_client
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.learner import Learner
from app.models.learning import LearningEvent

API = "/api/v1"

ATOMS_REPLY = json.dumps(
    {"atoms": [{"kind": "concept", "kc_ids": [], "md": "Sine is opposite/hypotenuse.", "provenance": {}}]}
)
RENDERED = "# Trig\n\nSine is opposite over hypotenuse."


@pytest.fixture
def fake_llm_refresh() -> Iterator[None]:
    """Distill reply then render reply — one full refresh."""
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(
        script=[FakeTurn(text=ATOMS_REPLY), FakeTurn(text=RENDERED)]
    )
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def _subject_topic_kc(api_client: AsyncClient) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:8]
    r = await api_client.post(f"{API}/subjects", json={"slug": f"s-{suffix}", "name": f"S {suffix}"})
    subject_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/subjects/{subject_id}/topics", json={"slug": f"t-{suffix}", "name": "Trig"}
    )
    topic_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs", json={"slug": f"k-{suffix}", "name": "Sine"}
    )
    return subject_id, topic_id, r.json()["id"]


async def _make_stale(db_session: AsyncSession, kc_id: str) -> None:
    """Insert an observation for the request-scoped dev learner (created by the first request)."""
    learner = await db_session.scalar(select(Learner).where(Learner.handle == DEV_LEARNER_HANDLE))
    assert learner is not None
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=uuid.UUID(kc_id),
            event_type="observation",
            payload={"score": 0.0, "hints_used": 0, "item_id": None, "response": None},
        )
    )
    await db_session.flush()


async def test_get_note_empty_not_stale(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.get(f"{API}/topics/{topic_id}/note")
    assert r.status_code == 200
    data = r.json()
    assert data["content_md"] is None and data["stale"] is False
    assert data["effective_format"] == "outline"


async def test_refresh_distills_and_renders(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm_refresh: None
) -> None:
    subject_id, topic_id, kc_id = await _subject_topic_kc(api_client)
    await _make_stale(db_session, kc_id)

    r = await api_client.get(f"{API}/topics/{topic_id}/note")
    assert r.json()["stale"] is True

    r = await api_client.post(f"{API}/topics/{topic_id}/note/refresh")
    assert r.status_code == 200
    data = r.json()
    assert data["content_md"] == RENDERED
    assert data["revision_ordinal"] == 1 and data["stale"] is False

    r = await api_client.get(f"{API}/subjects/{subject_id}/notes")
    entries = r.json()
    assert entries[0]["has_note"] is True and entries[0]["stale"] is False

    r = await api_client.get(f"{API}/topics/{topic_id}/note/revisions")
    assert [rev["cause"] for rev in r.json()] == ["distill"]


async def test_edit_404_without_note(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.put(f"{API}/topics/{topic_id}/note", json={"content_md": "hi"})
    assert r.status_code == 404


async def test_format_patch_validates(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.patch(f"{API}/topics/{topic_id}/note/format", json={"format": "haiku"})
    assert r.status_code == 422


async def test_unknown_topic_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/topics/{uuid.uuid4()}/note")
    assert r.status_code == 404


async def test_restore_unknown_revision_404(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.post(f"{API}/topics/{topic_id}/note/revisions/7/restore")
    assert r.status_code == 404
```

Note the KC-creation path: confirm the exact KC endpoint shape in `app/api/v1/knowledge.py`
(`POST /topics/{topic_id}/kcs`) before running; adjust the helper if the route differs.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_notes_api.py -v
```

Expected: all pass (6 tests).

- [ ] **Step 6: Full gate + commit**

```bash
uv run poe check
git add app/schemas/note.py app/api/v1/notes.py app/api/v1/__init__.py tests/test_notes_api.py
git commit -m "feat(notes): API endpoints (index, read, refresh, edit, format, history)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: `note_format` profile dimension

**Files:**
- Modify: `app/learning/profile_estimators.py`
- Test: `tests/test_note_format_dimension.py`

**Interfaces:**
- Consumes: `EstimatorContext`, `DimensionEstimate`, `DimensionSpec`, `DIMENSION_SPECS`, `Usage`; `Note` (Task 1)
- Produces: a `note_format` entry in `DIMENSION_SPECS` whose value is one of `FORMATS` (the service cascade in Task 3 already reads it by key)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_note_format_dimension.py`:

```python
"""note_format dimension: earned from real explicit format choices, never fabricated."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.profile_estimators import DIMENSION_SPECS, EstimatorContext, _estimate_note_format
from app.llm.registry import fake_llm_client
from app.models.knowledge import Subject, Topic
from app.models.learner import Learner
from app.models.note import Note


def _ctx(session: AsyncSession, learner_id: uuid.UUID) -> EstimatorContext:
    return EstimatorContext(
        session=session, learner_id=learner_id, events=[], messages=[], llm=fake_llm_client()
    )


async def _learner_with_notes(db_session: AsyncSession, formats: list[str | None]) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    for fmt in formats:
        topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
        db_session.add(topic)
        await db_session.flush()
        db_session.add(Note(learner_id=learner.id, topic_id=topic.id, format=fmt))
    await db_session.flush()
    return learner


async def test_registered_in_catalog() -> None:
    assert any(spec.key == "note_format" for spec in DIMENSION_SPECS)


async def test_under_threshold_emits_nothing(db_session: AsyncSession) -> None:
    learner = await _learner_with_notes(db_session, ["outline", "outline"])
    estimate, usage = await _estimate_note_format(_ctx(db_session, learner.id))
    assert estimate is None and usage.input_tokens == 0


async def test_majority_format_emitted(db_session: AsyncSession) -> None:
    learner = await _learner_with_notes(db_session, ["narrative", "narrative", "outline", None])
    estimate, _ = await _estimate_note_format(_ctx(db_session, learner.id))
    assert estimate is not None
    assert estimate.value == "narrative"
    assert 0.0 <= estimate.uncertainty < 1.0


async def test_no_majority_emits_nothing(db_session: AsyncSession) -> None:
    learner = await _learner_with_notes(
        db_session, ["narrative", "outline", "mnemonic", "worked_examples"]
    )
    estimate, _ = await _estimate_note_format(_ctx(db_session, learner.id))
    assert estimate is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_note_format_dimension.py -v
```

Expected: FAIL — `ImportError: cannot import name '_estimate_note_format'`.

- [ ] **Step 3: Implement the estimator**

In `app/learning/profile_estimators.py`, add near the other context/preference estimators
(match the file's section conventions), plus the import `from app.models.note import Note`
alongside the other model imports:

```python
# --- Context & preferences: note format -------------------------------------

NOTE_FORMAT_MIN_CHOICES = 3


async def _estimate_note_format(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    """The learner's settled note format, read from real explicit ``Note.format`` choices.

    No behavioral signal for written-material preference exists before notes ship, so this
    dimension is *earned* from the format toggle itself (spec §6.3): ≥ 3 explicit choices
    with a strict majority — value = the majority format, uncertainty = 1 − its share.
    Anything less emits nothing and the notes format-cascade falls through to heuristics.
    """
    rows = await ctx.session.scalars(
        select(Note.format).where(Note.learner_id == ctx.learner_id, Note.format.is_not(None))
    )
    choices = list(rows.all())
    if len(choices) < NOTE_FORMAT_MIN_CHOICES:
        return None, Usage()
    counts = Counter(choices)
    fmt, top = counts.most_common(1)[0]
    if top * 2 <= len(choices):  # need a strict majority, not a plurality
        return None, Usage()
    share = top / len(choices)
    return DimensionEstimate(value=fmt, uncertainty=round(1.0 - share, 2)), Usage()
```

If `Counter` isn't already imported in the file, add `from collections import Counter`.
Then register it in `DIMENSION_SPECS` (append, matching the existing entry style):

```python
    DimensionSpec(
        key="note_format", kind="trait", source="behavioral", estimate=_estimate_note_format
    ),
```

- [ ] **Step 4: Run the tests (plus the existing profile suites — the catalog changed)**

```bash
uv run pytest tests/test_note_format_dimension.py tests/test_profile.py tests/test_profile_estimators.py -v
```

Expected: new tests pass; existing profile suites stay green (a refresh now computes one more
dimension — if any existing test asserts an exact dimension count, update that expectation).

- [ ] **Step 5: Full gate + commit**

```bash
uv run poe check
git add app/learning/profile_estimators.py tests/test_note_format_dimension.py
git commit -m "feat(notes): note_format profile dimension earned from explicit choices

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Frontend API layer — `frontend/src/api/notes.ts`

**Files:**
- Create: `frontend/src/api/notes.ts`
- Modify: `frontend/package.json` (via `npm install`)

**Interfaces:**
- Consumes: `API_BASE_URL` from `./client`; TanStack Query
- Produces (Task 7 relies on these): types `NoteRead`, `NoteIndexEntry`, `NoteRevisionRead`,
  `NoteFormat`; hooks `useNotesIndex(subjectId)`, `useNote(topicId)`, `useRefreshNote(topicId)`,
  `useEditNote(topicId)`, `useSetFormat(topicId)`, `useRevisions(topicId, enabled)`,
  `useRevisionSource(topicId, ordinal | null)`, `useRestoreRevision(topicId)`

- [ ] **Step 1: Install react-markdown**

```bash
npm --prefix frontend install react-markdown
```

- [ ] **Step 2: Write the API module**

New endpoints aren't in the generated `schema.d.ts` (regeneration needs a running backend), so
this file uses raw `fetch` + hand-written types — the exact `onboarding.ts` precedent. Create
`frontend/src/api/notes.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_BASE_URL } from "./client";

// ============================================================================
// Types (mirror app/schemas/note.py)
// ============================================================================

export type NoteFormat = "outline" | "narrative" | "mnemonic" | "worked_examples";

export interface NoteRead {
  topic_id: string;
  content_md: string | null;
  format: NoteFormat | null;
  effective_format: NoteFormat;
  stale: boolean;
  revision_ordinal: number | null;
  updated_at: string | null;
}

export interface NoteIndexEntry {
  topic_id: string;
  topic_name: string;
  has_note: boolean;
  stale: boolean;
  updated_at: string | null;
}

export interface NoteRevisionRead {
  ordinal: number;
  cause: "distill" | "learner_edit" | "restore";
  created_at: string;
}

export interface NoteRevisionSource {
  ordinal: number;
  content_md: string;
}

// ============================================================================
// Fetch helper (new endpoints aren't in the generated schema.d.ts — same
// raw-fetch approach as onboarding.ts)
// ============================================================================

async function jfetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}/api/v1${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`${init?.method ?? "GET"} ${path} failed: ${res.status}`);
  }
  return (await res.json()) as T;
}

// ============================================================================
// Hooks
// ============================================================================

export function useNotesIndex(subjectId: string | null) {
  return useQuery({
    queryKey: ["notes", subjectId],
    queryFn: () => jfetch<NoteIndexEntry[]>(`/subjects/${subjectId}/notes`),
    enabled: subjectId !== null,
  });
}

export function useNote(topicId: string) {
  return useQuery({
    queryKey: ["note", topicId],
    queryFn: () => jfetch<NoteRead>(`/topics/${topicId}/note`),
  });
}

/** Shared: after any mutation the note, its history, and the index badges are all stale. */
function useInvalidateNote(topicId: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["note", topicId] });
    void qc.invalidateQueries({ queryKey: ["note-revisions", topicId] });
    void qc.invalidateQueries({ queryKey: ["notes"] });
  };
}

export function useRefreshNote(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: () => jfetch<NoteRead>(`/topics/${topicId}/note/refresh`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

export function useEditNote(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: (content_md: string) =>
      jfetch<NoteRead>(`/topics/${topicId}/note`, {
        method: "PUT",
        body: JSON.stringify({ content_md }),
      }),
    onSuccess: invalidate,
  });
}

export function useSetFormat(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: (format: NoteFormat | null) =>
      jfetch<NoteRead>(`/topics/${topicId}/note/format`, {
        method: "PATCH",
        body: JSON.stringify({ format }),
      }),
    onSuccess: invalidate,
  });
}

export function useRevisions(topicId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["note-revisions", topicId],
    queryFn: () => jfetch<NoteRevisionRead[]>(`/topics/${topicId}/note/revisions`),
    enabled,
  });
}

export function useRevisionSource(topicId: string, ordinal: number | null) {
  return useQuery({
    queryKey: ["note-revision-source", topicId, ordinal],
    queryFn: () =>
      jfetch<NoteRevisionSource>(`/topics/${topicId}/note/revisions/${ordinal}`),
    enabled: ordinal !== null,
  });
}

export function useRestoreRevision(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: (ordinal: number) =>
      jfetch<NoteRead>(`/topics/${topicId}/note/revisions/${ordinal}/restore`, {
        method: "POST",
      }),
    onSuccess: invalidate,
  });
}
```

- [ ] **Step 3: Build + lint**

```bash
npm --prefix frontend run build && npm --prefix frontend run lint
```

Expected: both clean (the module compiles even before any page consumes it).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/notes.ts frontend/package.json frontend/package-lock.json
git commit -m "feat(notes-ui): notes API hooks + react-markdown dependency

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Notes UI — pages, history drawer, routes, nav

**Files:**
- Create: `frontend/src/pages/Notes.tsx`
- Create: `frontend/src/pages/NoteView.tsx`
- Create: `frontend/src/components/notes/HistoryDrawer.tsx`
- Modify: `frontend/src/App.tsx` (two routes) and `frontend/src/components/NavBar.tsx` (nav link)

**Interfaces:**
- Consumes: every hook/type from Task 6; `SubjectPicker` (`components/SubjectPicker.tsx`,
  props `{subjects, selectedId, onSelect}`); `useSubjects` (`api/hooks.ts`); `react-markdown`
- Produces: routes `/app/notes` and `/app/notes/:topicId`

- [ ] **Step 1: Notes index page**

Create `frontend/src/pages/Notes.tsx`:

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen } from "lucide-react";
import { useSubjects } from "../api/hooks";
import { useNotesIndex } from "../api/notes";
import { SubjectPicker } from "../components/SubjectPicker";
import { PlaceholderPage } from "../components/PlaceholderPage";

export function Notes() {
  const { data: subjects, isLoading } = useSubjects();
  const [pickedId, setPickedId] = useState<string | null>(null);
  const selectedId = pickedId ?? subjects?.[0]?.id ?? null;
  const { data: entries } = useNotesIndex(selectedId);

  if (isLoading) {
    return <p className="text-caption text-base-content/50">Loading subjects…</p>;
  }

  if (!subjects || subjects.length === 0) {
    return (
      <PlaceholderPage
        icon={BookOpen}
        title="Notes"
        description="Your notes grow automatically as you study — add a subject to begin."
      />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-h1">Notes</h1>
      <SubjectPicker subjects={subjects} selectedId={selectedId} onSelect={setPickedId} />
      <ul className="flex flex-col gap-2">
        {entries?.map((entry) => (
          <li key={entry.topic_id}>
            <Link
              to={`/app/notes/${entry.topic_id}`}
              className="border-base-300 hover:bg-base-200 flex items-center justify-between rounded-field border px-4 py-3 transition-colors"
            >
              <span className="flex items-center gap-3">
                <BookOpen size={16} className="text-base-content/50" />
                {entry.topic_name}
              </span>
              <span className="flex items-center gap-2">
                {entry.stale && <span className="badge badge-warning badge-sm">new material</span>}
                {!entry.has_note && !entry.stale && (
                  <span className="text-caption text-base-content/40">no notes yet</span>
                )}
              </span>
            </Link>
          </li>
        ))}
        {entries?.length === 0 && (
          <p className="text-caption text-base-content/50">No topics in this subject yet.</p>
        )}
      </ul>
    </div>
  );
}
```

- [ ] **Step 2: History drawer component**

Create `frontend/src/components/notes/HistoryDrawer.tsx`:

```tsx
import { useState } from "react";
import { History, RotateCcw, X } from "lucide-react";
import {
  useRestoreRevision,
  useRevisions,
  useRevisionSource,
  type NoteRevisionRead,
} from "../../api/notes";

const CAUSE_LABELS: Record<NoteRevisionRead["cause"], string> = {
  distill: "Auto-updated from study",
  learner_edit: "Your edit",
  restore: "Restored",
};

interface Props {
  topicId: string;
  open: boolean;
  onClose: () => void;
}

/** Slide-over listing every revision; viewing shows the mechanical source, restore is a new
 * revision (history is never rewritten server-side). */
export function HistoryDrawer({ topicId, open, onClose }: Props) {
  const [viewing, setViewing] = useState<number | null>(null);
  const { data: revisions } = useRevisions(topicId, open);
  const { data: source } = useRevisionSource(topicId, viewing);
  const restore = useRestoreRevision(topicId);

  if (!open) return null;

  return (
    <aside className="border-base-300 bg-base-100 fixed inset-y-0 right-0 z-20 flex w-96 flex-col gap-4 overflow-y-auto border-l p-6 shadow-lg">
      <div className="flex items-center justify-between">
        <h2 className="text-h3 flex items-center gap-2">
          <History size={18} /> History
        </h2>
        <button type="button" className="btn btn-ghost btn-sm btn-square" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <ul className="flex flex-col gap-2">
        {revisions
          ?.slice()
          .reverse()
          .map((rev) => (
            <li key={rev.ordinal} className="border-base-300 rounded-field border p-3">
              <div className="flex items-center justify-between">
                <span className="text-caption">
                  #{rev.ordinal} — {CAUSE_LABELS[rev.cause]}
                </span>
                <span className="flex gap-1">
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs"
                    onClick={() => setViewing(viewing === rev.ordinal ? null : rev.ordinal)}
                  >
                    {viewing === rev.ordinal ? "Hide" : "View"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs"
                    disabled={restore.isPending}
                    onClick={() => {
                      if (window.confirm(`Restore revision #${rev.ordinal}? Your current note stays in history.`)) {
                        restore.mutate(rev.ordinal, { onSuccess: onClose });
                      }
                    }}
                  >
                    <RotateCcw size={12} /> Restore
                  </button>
                </span>
              </div>
              <p className="text-caption text-base-content/50">
                {new Date(rev.created_at).toLocaleString()}
              </p>
              {viewing === rev.ordinal && source && (
                <pre className="bg-base-200 text-caption mt-2 max-h-64 overflow-auto rounded-field p-2 whitespace-pre-wrap">
                  {source.content_md}
                </pre>
              )}
            </li>
          ))}
        {revisions?.length === 0 && (
          <p className="text-caption text-base-content/50">No revisions yet.</p>
        )}
      </ul>
    </aside>
  );
}
```

- [ ] **Step 3: Note page**

Create `frontend/src/pages/NoteView.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { ArrowLeft, History, Loader2, Pencil } from "lucide-react";
import {
  useEditNote,
  useNote,
  useRefreshNote,
  useSetFormat,
  type NoteFormat,
} from "../api/notes";
import { HistoryDrawer } from "../components/notes/HistoryDrawer";

const FORMAT_LABELS: Record<NoteFormat, string> = {
  outline: "Outline",
  narrative: "Narrative",
  mnemonic: "Mnemonics",
  worked_examples: "Worked examples",
};

export function NoteView() {
  const { topicId } = useParams();
  const id = topicId!;
  const { data: note } = useNote(id);
  const refresh = useRefreshNote(id);
  const edit = useEditNote(id);
  const setFormat = useSetFormat(id);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const refreshedRef = useRef(false);

  // Catch-up on read: one auto-refresh when the note arrives stale (ref-guarded — the
  // invalidation after refresh refetches the note, which must not loop).
  useEffect(() => {
    if (note?.stale && !refreshedRef.current && !refresh.isPending) {
      refreshedRef.current = true;
      refresh.mutate();
    }
  }, [note?.stale, refresh]);

  if (!note) {
    return <p className="text-caption text-base-content/50">Loading…</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <Link to="/app/notes" className="btn btn-ghost btn-sm">
          <ArrowLeft size={16} /> All notes
        </Link>
        <div className="flex items-center gap-2">
          <select
            className="select select-sm"
            value={note.format ?? "auto"}
            onChange={(e) => {
              const v = e.target.value;
              setFormat.mutate(v === "auto" ? null : (v as NoteFormat));
            }}
          >
            <option value="auto">Auto ({FORMAT_LABELS[note.effective_format]})</option>
            {(Object.keys(FORMAT_LABELS) as NoteFormat[]).map((f) => (
              <option key={f} value={f}>
                {FORMAT_LABELS[f]}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setHistoryOpen(true)}
          >
            <History size={16} /> History
          </button>
          {note.content_md !== null && !editing && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setDraft(note.content_md ?? "");
                setEditing(true);
              }}
            >
              <Pencil size={16} /> Edit
            </button>
          )}
        </div>
      </div>

      {(refresh.isPending || setFormat.isPending) && (
        <div className="alert alert-info flex items-center gap-2">
          <Loader2 size={16} className="animate-spin" /> Updating your notes…
        </div>
      )}
      {edit.isError && (
        <div className="alert alert-error">
          Edit could not be absorbed — your text is preserved below. Try saving again.
        </div>
      )}

      {editing ? (
        <div className="flex flex-col gap-3">
          <textarea
            className="textarea textarea-bordered min-h-96 w-full font-mono"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="flex gap-2">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={edit.isPending || draft.trim().length === 0}
              onClick={() => edit.mutate(draft, { onSuccess: () => setEditing(false) })}
            >
              {edit.isPending ? "Saving…" : "Save"}
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : note.content_md !== null ? (
        <article className="prose max-w-none">
          <ReactMarkdown>{note.content_md}</ReactMarkdown>
        </article>
      ) : (
        !refresh.isPending && (
          <p className="text-base-content/60">
            No notes for this topic yet — they'll appear automatically once you've studied it.
          </p>
        )
      )}

      <HistoryDrawer topicId={id} open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </div>
  );
}
```

- [ ] **Step 4: Routes + nav**

In `frontend/src/App.tsx`: add imports `import { Notes } from "./pages/Notes";` and
`import { NoteView } from "./pages/NoteView";`, then inside the `/app` `PageShell` route block
(alongside `lessons` / `dashboard` / `uploads`):

```tsx
          <Route path="notes" element={<Notes />} />
          <Route path="notes/:topicId" element={<NoteView />} />
```

In `frontend/src/components/NavBar.tsx`: add `BookOpen` to the lucide import and a link
entry after Lessons (`NotebookText` is Lessons' icon — Notes uses `BookOpen`):

```tsx
  { to: "/app/notes", label: "Notes", icon: BookOpen },
```

- [ ] **Step 5: Build + lint**

```bash
npm --prefix frontend run build && npm --prefix frontend run lint
```

Expected: both clean. If `prose` classes render unstyled (no typography plugin), replace
`className="prose max-w-none"` with `className="flex flex-col gap-3 [&_h1]:text-h1 [&_h2]:text-h2 [&_h3]:text-h3 [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:list-decimal [&_ol]:pl-6"` — check
whether any existing page already styles rendered markdown and match it.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/Notes.tsx frontend/src/pages/NoteView.tsx frontend/src/components/notes/ frontend/src/App.tsx frontend/src/components/NavBar.tsx
git commit -m "feat(notes-ui): notes index + note page (format switcher, edit, history)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Docs + final gates

**Files:**
- Modify: `docs/ROADMAP.md` (Phase 8 section: check off scope items, add the landed-note)

- [ ] **Step 1: Full backend + frontend gates**

```bash
uv run poe check
npm --prefix frontend run build && npm --prefix frontend run lint
```

Expected: everything green.

- [ ] **Step 2: ROADMAP landed-note**

In `docs/ROADMAP.md` Phase 8: flip the four scope checkboxes to ☑ and append a landed-note
(matching the voice of the other phases' notes) covering: the substrate/projection split and
why (format switching never re-distills; the toggle earns the `note_format` dimension);
catch-up-on-read + watermark (and the no-change path for sibling-topic staleness); the
learner-atom invariant enforced in code; revision history with mechanical source views;
absorb semantics (learner edits are the authority, may delete their own atoms); and the known
v1 gaps (topic-level granularity only; notes not yet a retrieval source; format switch-event
history deferred).

- [ ] **Step 3: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs: Phase 8 Notes landed

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review (completed at write time)

- **Spec coverage:** §3 models → Task 1 · §4 pipeline (staleness incl. render-heal, gather
  caps, distill invariant, no-change, watermark rules) → Tasks 2–3 · §5 absorb → Tasks 2–4 ·
  §6 formats/cascade/dimension → Tasks 3 + 5 · §7 history/restore/mechanical view → Tasks 3–4 ·
  §8 API table → Task 4 (all eight endpoints) · §9 UI → Tasks 6–7 · §10 roles/config/cost-log →
  Tasks 2–3 · §11 error table → Tasks 3–4 (each row has a test or explicit handling) · §12
  test list → the four test files. Live-Ollama smoke deliberately not a task: the existing
  live suites are skippable extras; add one only if the live walkthrough (post-plan) finds a
  prompt-shape problem.
- **Placeholder scan:** clean — every code step has complete code; the two "check the existing
  file first" notes (migration type spellings, models `__init__`) are verification
  instructions against known files, with the expected content stated.
- **Type consistency:** `NoteView` fields = `NoteRead` fields; `notes_index` dict keys =
  `NoteIndexEntry` fields; `DistillResult`/`parse_atoms_payload`/`assign_atom_ids` names match
  between Task 2's tests and implementation and Task 3's imports; frontend types mirror
  `app/schemas/note.py` exactly; `EPOCH` re-exported by the service and used by its tests.
