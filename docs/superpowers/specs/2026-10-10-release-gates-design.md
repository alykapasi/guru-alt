# End-to-end and release gates (S58) — design

Tracker item S58, workstream 6. Branch `feat/workstream-6`, one PR.

## Goal

Every v0 boundary a learner or administrator reaches has a browser journey, the suites pass
regardless of run order or clock drift, and `main` cannot take a red PR.

**Success:** three new journeys pass three runs in a row on a fresh database; the two recorded
order-dependent failures cannot recur; a branch rule requires the `CI` check and the PR for this
work shows itself blocked until CI passes.

**Not in scope:** real infrastructure (S60), real Clerk sign-in, model quality (S59),
calibration (S18).

## 1. Browser journeys

Three new spec files in `frontend/e2e/`, desktop project only (the phone project stays limited
to `a11y.spec.ts`). Sign-in uses `journey.ts`'s `signIn`; an administrator is made with
`poe grant-admin`, as `admin.spec.ts` does.

### 1.1 `admin-visit.spec.ts` — audited administrator visit (P10)

1. A learner signs in and starts a conversation with one message.
2. A second account signs in and is granted admin.
3. In the portal, "View" on the learner opens the reason dialog. Submitting it empty shows
   "Say why, in a sentence." With a reason, the banner reads "Admin access to *handle*'s account".
4. During the visit the learner's conversation is listed, and the administrator sends one
   message in it.
5. "Stop viewing" removes the banner and returns the administrator to their own account. The
   portal's visit log shows the visit, its reason, and the recorded action.
6. In the learner's own session, the administrator's message is shown as sent by an
   administrator, and the learner's practice evidence is unchanged.

`scripts/e2e-backend.sh` exports `GURU_IMPERSONATION_ENABLED=true`. The existing admin journey
must not depend on the switch being off; the plan checks this.

### 1.2 `web-restriction.spec.ts` — no web addresses in v0 (S30)

1. The library page offers file upload and no field for a web address.
2. `POST /api/v1/sources/link` from the signed-in browser context answers 403, and the library
   still lists the learner's uploaded file.
3. During an agentic-mode turn the browser requests nothing outside the app and API origins
   (Playwright request capture). This does not cover server-side fetches; the backend tests in
   `tests/test_v0_web_policy.py` do.

### 1.3 `notes.spec.ts` — exact note edits (S40)

1. A learner with a subject and a generated note opens it, edits it to an exact string
   (irregular spacing and a line break), saves, reloads: the text is identical.
2. With the editor open, the note is changed through the API (a new revision). Saving shows
   the conflict message ("Reload it and reapply your changes"), and the editor still holds the
   draft.

Any model prompt these journeys reach that the stand-in provider cannot answer gets a shape in
`app/llm/providers/shaped.py`, with a prompt-coupling test.

## 2. Order and clock independence

### 2.1 Fresh e2e database

`tests/testdb.py` gains `--fresh`: drop the database (`WITH (FORCE)`), recreate, migrate. It
refuses unless the database name ends in the requested suffix and the suffix is `_e2e`, so dev
and suite databases can never be dropped by it. `scripts/e2e-backend.sh` passes `--fresh`.

Effect: every backend start begins empty, as CI does, so the admin journey's "Not measured"
assertion holds whatever ran before it. Limit: a local run that reuses an already-running
backend (`reuseExistingServer`) keeps that backend's database.

### 2.2 Mastery test clock

`tests/test_mastery.py::test_recent_attempts_counts_sittings_not_rows` compares an event
timestamp written by Postgres `now()` against the app clock. The test reads the database's own
time after the write and passes it as `now`, so drift between the two clocks cannot flip the
`within_minutes=0` assertion. Test-only: in the app the same drift is milliseconds against a
30-minute window.

## 3. Required check on `main`

A branch protection rule, applied with `gh api`:

- required status check `CI` (the aggregate job in `ci.yml`), not strict (a PR need not be
  rebased onto the latest `main`);
- administrators not enforced, so the owner's direct docs pushes keep working;
- no required reviews;
- force pushes and branch deletion disallowed.

Verification: the settings read back from the API, and this work's PR shown as blocked while
CI is pending. OPERATIONS ("Deploying a release") records the rule and that changing the `CI`
job's name requires updating it.

## 4. Verification and records

- Each new journey: three consecutive local runs on a fresh database; then the full browser
  suite once, the way CI runs it.
- `poe check`, `poe format-check`, frontend test, lint and build.
- The PR's full CI run is the "verify changed boundaries together" evidence.
- Tracker: S58 to Completed with what ran; the S58 notes drop the two fixed failures.
- RUNBOOK: the e2e database is recreated per backend start; impersonation is on in e2e.

**Still unmeasured after this:** behaviour on real infrastructure and with real Clerk; anything
about learning effect.
