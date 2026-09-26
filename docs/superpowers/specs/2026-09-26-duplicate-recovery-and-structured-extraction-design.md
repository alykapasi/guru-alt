# Duplicate recovery, structure-aware chunking and reading notes (S77, S27)

**Status:** approved in conversation 2026-09-26; this document records it.
**Tracker:** S77 (duplicate-source recovery), S27 (technical extraction quality). Builds on the
versioned-sources slice (S29/S50): `PIPELINE_VERSION`, superseding, `poe reindex`.

## Problem

- **Stranded duplicates (S77).** An upload whose text matches a DONE source of the learner in
  the same subject/topic scope is stored as `meta.duplicate_of` with no chunks; only the
  original is searched. If the original stops standing in — moved to another subject by a
  curriculum commit, deleted (account cleanup today, a delete endpoint later), or left without
  current chunks — the duplicate silently grounds nothing while showing as a finished source.
- **Cut blocks (S27).** Chunking keeps line structure but windows at ~1000 characters, so a
  code block, table or display equation longer than a window is cut in half: the second half of
  a table has no header, a derivation loses its start.
- **Unread indicators (S27).** Extraction damage indicators are stored on every chunk and read
  by nothing. As written, `any_indicator` also fires on ordinary English (single-letter words
  count as isolated letters), so it cannot be surfaced as-is.

Accuracy evaluation against known-correct fixtures and the scanned/digital pair evaluation are
testing-phase work and not part of this slice.

## Decisions (from the design conversation)

1. **Duplicate recovery: react at the known causes *and* sweep** (choice C).
2. **Long structured blocks: whole up to 4× the window, split cleanly beyond** (choice A).
3. **Uncertainty: surface only facts — reading method and undecodable characters — to both the
   tutor and the learner** (choice A). Ratio indicators stay stored and unused until calibrated.

## Design

### 1. Duplicate recovery (S77)

**Migration 0065:** `sources.duplicate_of_id uuid NULL REFERENCES sources(id) ON DELETE SET NULL`,
indexed. Backfilled from `meta->>'duplicate_of'` where that id names an existing source.
`pipeline.run` writes the column (and no longer writes the meta key; old rows keep it,
unread).

**The invariant.** A DONE source with no current chunks (`superseded_at IS NULL`) is by
construction a text duplicate — a real extraction yields at least one chunk or fails. It is
*healthy* only while `duplicate_of_id` names a source that is DONE, has the same `learner_id`,
`subject_id` and `topic_id`, and has at least one current chunk. Anything else is *stranded*.

**`release_duplicates(session, source_ids) -> list[uuid.UUID]`** (`app/services/ingestion.py`):
for each given source that is a DONE, chunkless source, clear `duplicate_of_id`, set PENDING,
reset `attempts`, `error` and `lease_expires_at`. Flushes; returns the ids released. The caller
dispatches them (or leaves them for the reconcile sweep, which requeues stale PENDING sources).
A released source re-ingests from its **own** stored blob; the twin check (which requires the
twin to have current chunks) suppresses it again only if a valid original really exists.

**Instant, at the known causes:**

- **Curriculum commit reassignment** (`knowledge.create_curriculum`'s reassign loop): for each
  moved source, its duplicates (sources whose `duplicate_of_id` is the moved id) and, if the
  moved source is itself a duplicate, the moved source. Released in the same transaction; the
  API route dispatches the returned ids after commit with the ingestion enqueuer.
  `CurriculumResult` gains `released_source_ids: list[uuid.UUID]`.
- **Reindex scope repair** (`reindex.apply`): the same rule for every repaired source; dispatched
  with the run's enqueuer.
- **Deletion:** nothing to call — the foreign key nulls `duplicate_of_id` and the sweep finds it.

**Safety net:**

- `stranded_duplicates(session, learner_id=None) -> list[uuid.UUID]` returns every source that
  breaks the invariant.
- `reconcile_stranded` calls it, releases and requeues them; `ReconcileReport` gains
  `recovered: int`; the operator CLI (`poe reconcile-ingestion`) prints it.
- `reindex.plan` lists them as a fourth group, `stranded` (`StaleSource` entries, `chunks = 0`);
  `--apply` releases and dispatches them. Not counted against `--limit` (no model cost until the
  worker re-ingests, and then through its own budgets).

**Frontend:** `SourceRead` gains `duplicate_of_id`. A DONE row with a `duplicate_of_id` shows
"Same text as *origin*" (looked up in the list the page already loaded) or "Same text as another
of your sources" when the original is not in that list.

### 2. Structure-aware chunking (S27)

**`app/rag/structure.py`:** `segment(text: str) -> list[Segment]`, where
`Segment(kind: Literal["prose", "code", "table", "math"], text: str, start: int, fence: str | None,
header: str | None)`, over already-normalized text, line by line:

- **code:** a line starting with ```` ``` ```` or `~~~` opens a block (the rest of that line is
  the language tag, kept in `fence`); the next line starting with the same fence closes it. An
  unclosed fence runs to the end of the text.
- **table:** two or more consecutive lines whose first non-space character is `|`. `header` is
  the first line, plus the second when it is a separator (only `|`, `-`, `:`, spaces).
- **math:** `$$` … `$$` (a line consisting of `$$`, or starting a block), `\[` … `\]`, and
  `\begin{E}` … `\end{E}` for `E` in `equation`, `align`, `gather`, `multline`, `eqnarray`, each
  optionally starred. Unclosed runs to the end.
- Everything else is prose. Segments are contiguous and cover the text exactly.

**`chunk_units`:** per unit, normalize, then per segment:

- **prose** → `_windows` exactly as today. A unit with no blocks produces chunks identical to the
  current implementation (pinned by a regression test).
- **block ≤ `BLOCK_CAP` (= 4 × size, 4000 chars)** → one chunk, never cut.
- **block > `BLOCK_CAP`** → split at line breaks into parts of at most `BLOCK_CAP` characters
  *including* the repeated context: each table part starts with `header`; each code part is
  wrapped in its opening fence line and closing fence; each math part is wrapped in its opening
  and closing delimiter lines. A single line longer than the cap is hard-cut (nothing better
  exists).
- **Locator:** every block chunk gets `structure: "code" | "table" | "math"`; split blocks also
  get `part: "i/n"`. `char_start` is kept (offset of the segment or part in the unit's
  normalized text).

**`PIPELINE_VERSION = 2`.** Existing sources become re-extract candidates in `poe reindex`; the
operator runs `--apply --reextract` when ready (citations survive via superseding).

### 3. Reading notes (S27)

**`reading_note(provenance: dict) -> str | None`** (`app/rag/extraction_quality.py`), from facts
only:

- `method == "ocr"` → "read from a scan or image; wording may contain errors"
- `method == "asr"` → "transcribed from audio; wording may contain errors"
- `provenance["extraction"]` with `replacement_chars > 0` or `control_chars > 0` → "some
  characters could not be read"
- several → joined with "; "; none → `None`.

The indicators are measured per extracted unit (e.g. a page), so the note describes the unit the
chunk came from — accurate for every chunk of it.

**Tutor:**

- `grounding.format_grounding` renders a noted passage as `[N] (note) text`. When any passage
  carries a note, the instruction gains: "Passages marked with a reading note were machine-read
  and may contain errors: do not present exact figures, names or formulas from them as certain,
  and say so where it matters."
- `content._build_prompt` renders its snippets the same way; `generate_block` appends the same
  sentence to the grounded system prompt when any snippet carries a note.

**Learner:** `ChunkRead.reading_note: str | None` (computed from `provenance`); the citation pane
shows it beneath the passage.

The ratio indicators (`isolated_letter_ratio`, `vowelless_word_ratio`, `runaway_token_ratio`)
stay stored and unread; S18 records that they need thresholds from the testing phase.

## Testing

- **S77:** migration backfill (existing original → column set; missing original → NULL);
  pipeline writes `duplicate_of_id`; reassigning an original releases its duplicate;
  reassigning a duplicate releases it; deleting an original nulls the column and the sweep
  releases the duplicate; a healthy duplicate is left alone by the sweep; `reindex.plan` lists
  stranded duplicates and `apply` releases and dispatches them; a released duplicate
  re-ingests from its own blob and ends with current chunks; the curriculum-commit route
  dispatches released ids.
- **Chunking:** each block kind detected, including an unclosed fence and starred environments;
  a block under the cap is one chunk even when longer than `size`; an oversize table repeats its
  header in every part; oversize code is re-fenced with its tag; oversize math is re-wrapped;
  every part ≤ `BLOCK_CAP`; locators carry `structure` and `part`; prose-only text chunks
  exactly as before; `PIPELINE_VERSION == 2`.
- **Reading notes:** `reading_note` for OCR, ASR, replacement/control characters, combinations
  and clean text; grounding and lesson prompts carry notes and the extra sentence only when a
  passage is noted; `ChunkRead.reading_note`; citation pane shows it (vitest).
- **Frontend:** a duplicate row shows "Same text as …" with and without the original in the list.

## Out of scope

Accuracy evaluation against fixtures; scanned/digital pair evaluation; calibrating the ratio
indicators; OCR confidence from the vision model; per-sentence notes; tab-separated table
detection; attaching captions or lead-in sentences to blocks.

## Tracker updates on completion

- S77 → Partial; remaining: evaluate selected scanned/digital pairs (testing phase).
- S27 → Partial; remaining: fixture accuracy evaluation and ratio-indicator calibration
  (testing phase; S18).
- S18: add the three unread extraction ratio indicators as needing thresholds.
