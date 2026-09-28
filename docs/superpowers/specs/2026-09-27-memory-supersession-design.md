# Memory supersession and forget scope (S42)

**Status:** approved in conversation 2026-09-27; this document records it.
**Tracker:** S42 (remaining work). Workstream 4, slice D. Implements decision B from slice A
(`docs/superpowers/specs/2026-09-26-archive-delete-forget-design.md`).

## Problem

Write-back (`app/services/memory.py` `write_back`) compares each extracted memory with the
learner's nearest same-kind memory by embedding distance, with one threshold
(`memory_dedup_max_distance = 0.05`):

- a deleted memory that close suppresses it — learner-wide, however it was deleted;
- a current memory that close with identical text is a duplicate; with different text it is
  superseded;
- anything further is stored alongside.

Distance cannot tell "this updates that" from "this is another thing like that". 0.05 is almost
identical wording, so a real contradiction ("studies in the mornings" → "now studies in the
evenings") usually lands further apart and both stay current — the tutor is told two
incompatible things. Two genuinely different near-identical facts can be merged. And decision B
(2026-09-27) says forgetting a conversation's memories must suppress re-extraction only from that
same conversation, while today every tombstone is learner-wide. Superseded rows are invisible, so
a wrong supersession cannot be noticed or undone.

## Decisions (from the design conversation)

1. **A separate FAST judge, only when something is close** (choice B): a batched judge call
   classifies each candidate against its near neighbours as *same*, *updates* or *coexists*.
2. **A replacement is visible and undoable** (choice A).
3. **Forget scope per decision B:** single-memory Forget and "Forget everything" stay
   learner-wide; forgetting a conversation's memories suppresses only that conversation.

## Design

### The judge — `app/memory/supersession.py`

For each extracted item, in order. Items settled without the judge (steps 1–3) are added as
they are settled, so a later item sees an earlier one; items sent to the judge are decided
together after its one call, so two judged items in the same batch are not compared with each
other (extraction already asks for distinct items).

1. **Suppression** (below): a matching forgotten memory within 0.05 skips the item.
2. **Free duplicate:** the nearest current same-kind memory within 0.05 with identical text
   (after strip) skips the item, with no model call — as today.
3. **Neighbours:** up to 3 current same-kind memories within `memory_related_max_distance`
   (new setting, default 0.25, uncalibrated). `summary` items are never judged — what was covered
   always coexists. No neighbours → stored.
4. **One batched FAST call per write-back**, only if some item has neighbours. For each candidate
   the prompt gives the new statement and its numbered neighbours; neighbour text (extracted from
   conversation text) is fenced with `as_untrusted`. The reply is JSON: one verdict per candidate
   — `same`; `updates` with the neighbour's number; `coexists`.
5. **Applying:** `same` → skip. `updates` → store the new memory and mark the target
   `SUPERSEDED` with `superseded_by_id` = the new memory (as today). `coexists` → store.
   An unparseable reply, a missing verdict, or a neighbour number out of range is `coexists`:
   doubt never deletes.
6. The judge's call is logged per LLM call (role FAST, same conversation) like extraction.

Behaviour change: within 0.05 but different wording no longer auto-supersedes — the judge
decides; 0.05–0.25 is now judged instead of blindly coexisting.

### Forget scope — migration 0069

- `memories.forgotten_scope text NULL` — set only on `DELETED` rows: `learner` or `conversation`.
- Backfill: every existing `DELETED` row gets `learner` (how it was deleted is unknown; this is
  today's behaviour).
- Writers: `delete_memory` and `delete_all_memories` → `learner`;
  `removal.delete_conversation(forget=True)` and `removal.forget_conversation_memories` →
  `conversation`.
- Suppression check: a `DELETED` memory within 0.05 suppresses when `forgotten_scope = 'learner'`
  **or** (`forgotten_scope = 'conversation'` and its `origin_conversation_id` is the conversation
  being extracted from). Another conversation can teach the fact again, as a new memory.

### Undo a replacement

`POST /memory/{memory_id}/undo-replacement`, where `memory_id` is the current memory that
replaced another:

- the replaced memory (the most recent row whose `superseded_by_id = memory_id` and status
  `SUPERSEDED`) becomes `CURRENT` again with `superseded_by_id` cleared;
- the replacing memory becomes `SUPERSEDED` with `superseded_by_id` = the restored one — retired,
  not forgotten, so nothing is suppressed and a later mention is judged again.
- 404: unknown id or another learner's memory. 409 `not_a_replacement`: the memory is not
  current, or nothing current-able is superseded by it (it replaced nothing, or the old row has
  since been corrected or forgotten).
- A learner's own correction (`correct_memory`) supersedes the same way; undo reverts it.

### What the learner sees

- `MemoryRead.replaced: {id, content} | null` — the memory this one superseded (most recent, if
  more than one).
- The Memory page shows "Replaced: *old text*" under such a memory, with **Undo**.
- `list_memories` still lists current memories only; retrieval still uses current only.

## Errors

- Judge failure (timeout, provider error, bad JSON, no model configured): every item it covered
  is `coexists`; write-back still commits and advances its watermark. The job never fails
  because of the judge.
- Undo: 404 / 409 as above.

## Testing

All with the fake LLM and scripted judge replies.

- Judge module: parses the three verdicts; missing verdict, out-of-range neighbour and bad JSON →
  `coexists`; neighbour text fenced as untrusted; no neighbours → no call.
- Write-back: `updates` supersedes the named neighbour with the link; `coexists` keeps both;
  `same` skips; an identical duplicate within 0.05 is skipped with no judge call; summaries never
  judged; a judge failure keeps both and advances the watermark; the judge call is logged.
- Forget scope: after a conversation-scoped forget, the same fact is suppressed from that
  conversation but stored from another; after a single-memory Forget or "Forget everything" it
  is suppressed from any; migration backfills `learner`.
- Undo: restores the old and retires the new without suppression; the fact can be extracted
  again; 409 on a memory that replaced nothing or whose old row changed since; 404 for another
  learner; undo of a correction.
- API/frontend: `MemoryRead.replaced` populated; vitest for "Replaced: …" and Undo.

## Out of scope

Calibrating the 0.25 radius and measuring the judge's accuracy (testing phase); a Jev question
for this judgement; showing the supersession chain beyond one step.

## Tracker updates on completion

- S42 → **Implemented**: supersession is an explicit same/updates/coexists judgement, visible and
  undoable; conversation-scoped forgetting suppresses only that conversation.
