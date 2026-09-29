# Transfer evidence (S14, transfer half)

**Status:** approved in conversation 2026-09-29; this document records it.
**Tracker:** S14 (its last remaining half). Workstream 2, piece 4 of 4 (after grading
provenance, declared-check criteria and retention checks). Reintroduces what the
[goal-policy design](2026-09-22-goal-policy-design.md) §4.3 removed.

## Problem

`transfer_shown` was deleted because two item ids for one component — often one template, one
generator — are not "genuinely different applications". The goal-policy design said transfer
returns "when items can be shown to differ in a way the system can point at". Nothing records
how two questions differ, and nothing arranges for a question that does.

## Decisions (from the design conversation)

1. **What "different" means (A):** a *named setting* the component has not been practised in.
   The system can point at it: "applied in money, after learning it in the abstract".
2. **Where settings come from (1):** a fixed catalogue. Every generated question can be given one;
   an existing question with none is **abstract**. "Different" is an exact comparison, not a
   model's wording or judgement.
3. **When it is asked (1):** after retention is shown — transfer is claimed only about something
   learned and kept — as a cold check in the review queue, reusing the retention-check
   machinery.

## Design

### Settings

`app/learning/transfer.py`:

- `SETTINGS: tuple[tuple[str, str], ...]` — `(name, gloss)`, fixed order: `abstract` (no
  real-world setting), `everyday` (household, routines, shopping), `money` (prices, budgets,
  interest or trade), `physics`, `biology`, `engineering`, `computing`, `sport`, `health`,
  `society` (people, history, government), `arts` (music, art, writing), `nature` (weather,
  geography, ecology) — glosses written in the module.
- `ABSTRACT = "abstract"`; `setting_of(item_setting: str | None) -> str` maps NULL to abstract.
- `next_setting(practised: set[str]) -> str | None` — the first catalogue setting, in order,
  that is not `abstract` and not practised.

Migration `0073_item_settings` adds `items.setting` (Text, nullable). NULL is abstract: no
backfill. `Item.setting: Mapped[str | None] = mapped_column(Text, default=None)`.

Every generator in `app/learning/item_generation.py` takes `setting: str | None = None`. With a
setting, its system prompt gains "Set the question in this setting: <name> (<gloss>)." and the
item records it; without one the prompt is unchanged and NULL is stored.

### What counts as transfer

A component has shown transfer when some attempt at it was **unaided** (`_unassisted_clause`,
including piece 3's taught-first exclusion), **judged** (`observation`), **correct**
(`payload.correct` true), and on an item whose setting did **not** occur in any *earlier* attempt
at that component (assisted, unaided or self-rated; by `observed_at`). Items no longer present
count as abstract.

`KCEvidence` gains:

- `practised_settings: frozenset[str]` — settings of every attempted item;
- `transfer_setting: str | None` — the setting of the earliest attempt that showed transfer;
- `transfer_shown` — a property, `transfer_setting is not None`.

Computed in `kc_evidence` from one extra query over the same events joined to `items.setting`
(left join; a deleted item is abstract), ordered by time, per component.

Transfer is **evidence only**: the achievement rule, goal status and mastery are unchanged
(V02 stands).

### When a check is due — `mastery.due_transfer_checks`

`due_transfer_checks(session, learner_id, *, now=None) -> list[TransferCheck]` (same shape as
`RetentionCheck`), components where:

1. retention is shown;
2. transfer is not;
3. `next_setting(practised_settings)` is not None;
4. at least `retention_min_days` have passed since the component's latest judged attempt (so it
   does not follow straight on from other practice, and a failed check waits before the next).

`due_at` = latest judged attempt + `retention_min_days`, aware UTC. Derived, never stored.

### The queue

- `DueReviews` gains `transfer_checks: frozenset[uuid.UUID]`; `_due_review_kc_ids` merges
  transfer checks like retention checks. A component due both is a retention check (it comes
  first).
- `revise_steps(..., transfer_check_kc_ids=...)`; `StepDict.transfer_check`;
  `PlanGroundingContext.transfer_check`. Guided practice runs it cold, exactly as a retention
  check (`cold = check_first or retention_check or transfer_check`).
- `ReviewItem.kind` / `ReviewItemRead.kind` gain `"transfer_check"`; `due_review_items` merges
  them (`_with_retention_checks` generalised) and resolves the item below.
- The item: `session_runner.transfer_item_for_kc(session, llm, *, learner_id, kc)` —
  `setting = next_setting(practised)`; an unseen SHORT item the learner can see with that
  setting, else one generated in it (`generate_short_item(..., setting=...)`). Guided practice
  (`workflow`), the session surface (`next_item`) and the review endpoint use it for a flagged
  step or entry.
- A failed check makes that setting practised, so the next check picks the next one.

### Shown back

- `app/schemas/analytics.py`: `transfer_shown: bool`, `transfer_setting: str | None` per
  component; `app/services/analytics.py` fills them from `KCEvidence`.
- `MasteryEvidence.tsx`: "applied in <setting>" when shown; "not yet applied in a new setting"
  when retention is shown but transfer is not; nothing otherwise.
- `ReviewsDueCard`: "Transfer check" badge for that kind.
- API types regenerated; contract checked.

## Errors

- Generation fails: no item; the step waits like any review without one.
- Every setting practised: never due (condition 3).
- A component the learner can no longer see: skipped (`_kcs_authorized`).

## Testing

- Rule: unaided correct answer in an unpractised setting shows transfer and names it; a practised
  setting, a wrong answer, a hinted one and a taught-first one do not; an abstract-only
  (NULL-setting) history then a correct `money` answer shows it.
- Due: after retention; not before `retention_min_days` since the latest attempt; not once shown;
  not when every setting is practised; after a failed `money` check the next item is in the next
  setting.
- Generators: a setting reaches the prompt and the item; none leaves both as before.
- Queue: a due transfer check is a flagged cold step; `/reviews/due` returns
  `kind: "transfer_check"` with an item in the chosen setting.
- Migration 0073 (existing items NULL); `db-check` clean.
- Analytics carries `transfer_shown`/`transfer_setting`; vitest for the evidence line and badge.
- The shaped fake provider still recognises every generator prompt with a setting line.

## Docs

Tracker S14: Implemented. Goal-policy design §4.3 and §11: pointers here. CLAUDE.md achievement
bullet: transfer is an unaided correct answer in a catalogue setting not practised before, shown
as evidence and not required for achievement. No RUNBOOK change.

## Out of scope

Varying settings in ordinary practice; transfer as an achievement requirement; settings beyond
the catalogue; checks after transfer is shown; cross-subject reuse (S24).
