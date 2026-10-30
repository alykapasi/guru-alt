# End-to-end and release gates (S58) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Browser journeys for the three uncovered v0 boundaries, suites that pass in any order and under clock drift, and a required `CI` check on `main`.

**Architecture:** Three new Playwright specs in `frontend/e2e/` drive the real built bundle against the e2e API and worker (`scripts/e2e-backend.sh`); the stand-in model (`app/llm/providers/shaped.py`) gains a note-distill shape so notes can be generated offline. The e2e database is recreated per backend start (`tests/testdb.py --fresh`). A GitHub branch rule requires the aggregate `CI` job.

**Tech Stack:** Playwright 1.x + axe (`e2e/a11y.ts`), React 19, FastAPI, pytest, `gh api`.

**Spec:** `docs/superpowers/specs/2026-10-10-release-gates-design.md`

## Global Constraints

- Branch `feat/workstream-6`, one PR. Commit subjects end `[S58]`. Trailer exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never reset, amend, rebase, squash or force-push. Stage files by name; run `git status` after each commit.
- No paid model calls: journeys run on the `shaped` provider only; backend tests on `FakeProvider`.
- New journeys run in the `desktop` project only (the `phone` project keeps `testMatch: /a11y\.spec\.ts/`).
- Run journeys keyless: `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test …` from `frontend/` (CI=1 makes Playwright start its own servers, so `--fresh` applies).
- `--fresh` may only ever drop a database whose name ends in `_e2e`.
- Never print or read `.env` / `frontend/.env.local`. Never print a DSN (it carries the password).
- Branch rule: required check `CI`, `strict: false`, `enforce_admins: false`, no required reviews, force pushes and deletion disallowed.

## Review Focus

1. A stale backend left running locally (`reuseExistingServer`) — the fresh database is skipped; the RUNBOOK must say so, and `CI=1` runs must not reuse.
2. `--fresh` pointed at a non-e2e database (a mistyped suffix, or `GURU_DATABASE_URL` already naming `guru_test`) — must refuse before any DROP (Task 1 tests both).
3. The administrator's visit surviving a page reload — it does not (the visit token is in memory); the journey must navigate in-app only, and must assert the banner is gone after "Stop viewing", not after a reload.
4. A note editor with no accessible name — axe in the editing state must pass (Task 5 adds the label after a RED axe run).
5. A journey that silently passes because the model call fell through to prose — the distill shape needs a coupling test against the real `DISTILL_SYSTEM_PROMPT` (Task 5).

---

### Task 1: Fresh e2e database per backend start

**Files:**
- Modify: `tests/testdb.py`
- Modify: `scripts/e2e-backend.sh`
- Create: `tests/test_testdb.py`

**Interfaces:**
- Produces: `tests.testdb.refuse_unless_e2e(url: str) -> str` (returns the database name or raises `ValueError`); `python -m tests.testdb --suffix _e2e --fresh`.

- [ ] **Step 1: Write the failing test**

```python
"""`--fresh` drops a database, so it must only ever reach the browser journeys' one (S58)."""

import pytest

from tests.testdb import E2E_SUFFIX, refuse_unless_e2e

BASE = "postgresql+asyncpg://u:p@localhost:5433/guru"


def test_the_e2e_database_may_be_recreated() -> None:
    assert refuse_unless_e2e(f"{BASE}{E2E_SUFFIX}") == f"guru{E2E_SUFFIX}"


@pytest.mark.parametrize("name", ["guru", "guru_test", "guru_perf", "guru_e2e_old", "e2e"])
def test_any_other_database_is_refused_before_anything_is_dropped(name: str) -> None:
    with pytest.raises(ValueError, match="refusing to drop"):
        refuse_unless_e2e(f"postgresql+asyncpg://u:p@localhost:5433/{name}")


def test_a_quoted_name_is_refused() -> None:
    with pytest.raises(ValueError):
        refuse_unless_e2e('postgresql+asyncpg://u:p@localhost:5433/x"_e2e')
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_testdb.py -q`
Expected: FAIL — `ImportError: cannot import name 'refuse_unless_e2e'`.

- [ ] **Step 3: Implement**

In `tests/testdb.py`, after `_create_if_missing`, add:

```python
def refuse_unless_e2e(url: str) -> str:
    """The database name, if ``--fresh`` may drop it; otherwise ``ValueError``.

    Dropping is only ever for the browser journeys' database, which nothing else writes to and
    every run recreates. Checked on the *resolved* URL, so a ``GURU_DATABASE_URL`` that already
    names the dev or suite database cannot slip through by being passed in suffixed form.
    """
    name = make_url(url).database or ""
    if not name.endswith(E2E_SUFFIX) or name == E2E_SUFFIX or '"' in name:
        raise ValueError(f"refusing to drop {name!r}: only a *{E2E_SUFFIX} database is recreated")
    return name


async def _drop(url: str) -> None:
    """Drop the database (after the guard), closing any connection a dead run left open."""
    name = refuse_unless_e2e(url)
    parsed = make_url(url)
    conn = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()
```

`"guru_e2e_old"` must be refused: it does not end in `_e2e`. In `main()`, document the flag in the docstring (`[--fresh]`) and, before `_create_if_missing`:

```python
    if "--fresh" in args:
        asyncio.run(_drop(url))
```

In `scripts/e2e-backend.sh`, change the derivation line to pass `--fresh`, and extend its comment block with one sentence: every start begins on an empty database, as CI does, so no journey depends on what an earlier run left behind (S58).

```bash
GURU_DATABASE_URL="$(uv run python -m tests.testdb --suffix _e2e --fresh --print-url)"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_testdb.py -q`
Expected: 7 passed.

- [ ] **Step 5: Prove it end to end**

Run (from `frontend/`): `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test e2e/admin.spec.ts --project=desktop --reporter=line`
Expected: 2 passed — including "Not measured", which failed locally before on the reused database.

- [ ] **Step 6: Commit**

```bash
git add tests/testdb.py tests/test_testdb.py scripts/e2e-backend.sh
git commit -m "test(e2e): each backend start recreates the journeys' database, as CI does [S58]"
```

---

### Task 2: The mastery test reads the database's clock

**Files:**
- Modify: `tests/test_mastery.py` (`test_recent_attempts_counts_sittings_not_rows`, ~line 355)

**Interfaces:** none.

- [ ] **Step 1: Show the flake deterministically**

Add a temporary probe at the end of the test (do not commit it): simulate the app clock five seconds behind Postgres.

```python
    lagging = datetime.now(UTC) - timedelta(seconds=5)
    assert (
        await mastery.recent_attempts_at_item(
            db_session, learner.id, item_id, within_minutes=0, now=lagging
        )
        == 0
    )
```

Run: `uv run pytest tests/test_mastery.py::test_recent_attempts_counts_sittings_not_rows -q`
Expected: FAIL (`1 == 0`) — the event's `created_at` (Postgres `now()`) is after a lagging app clock.

- [ ] **Step 2: Fix the test**

Remove the probe. Replace the final assertion with one that takes `now` from the database itself, after the write (`clock_timestamp()` is wall time; `now()` would be the transaction start, equal to the event's own timestamp):

```python
    # Meeting the question again in a later sitting is an independent demonstration. "Later"
    # is measured on the database's clock: the event's created_at is Postgres's now(), and
    # comparing it with this process's clock made the assertion flip under a few ms of drift.
    db_now = await db_session.scalar(select(func.clock_timestamp()))
    assert (
        await mastery.recent_attempts_at_item(
            db_session, learner.id, item_id, within_minutes=0, now=db_now
        )
        == 0
    )
```

Add `func` / `select` to the test's `sqlalchemy` import if absent, and `timedelta` is not needed after the probe is removed.

- [ ] **Step 3: Run**

Run: `uv run pytest tests/test_mastery.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_mastery.py
git commit -m "test(mastery): a later sitting is measured on the database's clock, not the app's [S58]"
```

---

### Task 3: Administrator visit journey

**Files:**
- Create: `frontend/e2e/admin-visit.spec.ts`
- Create: `frontend/e2e/admin.ts` (shared `grantAdmin`, moved from `admin.spec.ts`)
- Modify: `frontend/e2e/admin.spec.ts` (import `grantAdmin` from `./admin`)
- Modify: `frontend/e2e/journey.ts` (add `apiGet`)
- Modify: `scripts/e2e-backend.sh` (`export GURU_IMPERSONATION_ENABLED=true`)

**Interfaces:**
- Produces: `grantAdmin(email: string): void` in `e2e/admin.ts`; `apiGet<T>(page: Page, path: string): Promise<T>` in `e2e/journey.ts`.

- [ ] **Step 1: Move `grantAdmin` and add `apiGet`**

`e2e/admin.ts` gets `REPO`, `e2eDatabaseUrl()` and `grantAdmin()` exactly as they are in `admin.spec.ts` today (exported), with the doc comment explaining why it shells out. `admin.spec.ts` imports `grantAdmin` from `./admin` and drops its copies. In `journey.ts`:

```typescript
/** A GET as the signed-in learner — the read twin of `api`. */
export async function apiGet<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(`${API_BASE}/api/v1${path}`);
  expect(response.ok(), `GET ${path} → ${response.status()}`).toBeTruthy();
  return (await response.json()) as T;
}
```

- [ ] **Step 2: Write the journey**

```typescript
import { expect, test, type Browser, type Page } from "@playwright/test";

import { grantAdmin } from "./admin";
import { api, apiGet, signIn } from "./journey";
import { expectAccessible } from "./a11y";

/** An administrator's audited visit to a learner's account, end to end (P10, S58).
 *
 * The API's refusals and the attribution rules are pinned in the backend suite; what only a
 * browser shows is the visit as a person meets it — the reason asked for before anything opens,
 * the banner on every page while it lasts, the learner's own conversation reachable, and the
 * way back. The visit lives in page memory, so this journey moves around in-app only: a reload
 * would end the visit, which is a property, not something to work around.
 */

async function newPage(browser: Browser): Promise<Page> {
  return (await browser.newContext()).newPage();
}

test("an administrator views a learner's account for a stated reason, and it is recorded", async ({
  browser,
}) => {
  test.slow(); // grant-admin shells out to `uv run`

  // The learner, with a conversation of their own.
  const learner = await newPage(browser);
  await signIn(learner);
  const title = `Visit me ${Date.now()}`;
  const conversation = await api<{ id: string }>(learner, "post", "/conversations", { title });
  await learner.goto(`/app/chat/${conversation.id}`);
  const first = learner.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await learner.getByPlaceholder("Message Guru").fill("My upload never became a lesson.");
  await learner.getByRole("button", { name: "Send" }).click();
  await (await first).finished();

  // The administrator.
  const admin = await newPage(browser);
  const account = await signIn(admin);
  grantAdmin(account.email);
  await admin.goto("/app/chat");
  await admin.getByRole("link", { name: "Admin" }).click();

  const me = await apiGet<{ email: string }>(learner, "/me");
  const row = admin.getByRole("row").filter({ hasText: me.email });
  await row.getByRole("button", { name: "View as" }).click();

  // No reason, no visit.
  await admin.getByRole("button", { name: "Start viewing" }).click();
  await expect(admin.getByText("Say why, in a sentence.")).toBeVisible();
  await admin.getByLabel("Why are you looking?").fill("They report their upload never became a lesson");
  await admin.getByRole("button", { name: "Start viewing" }).click();

  const banner = admin.getByRole("status").filter({ hasText: "Admin access to" });
  await expect(banner).toBeVisible();

  // The learner's conversation, reached in-app, and one message sent inside it.
  await admin.getByRole("link", { name: "Chat" }).click();
  await admin.getByRole("link", { name: title }).click();
  await expect(admin.getByText("My upload never became a lesson.")).toBeVisible();
  const sent = admin.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await admin.getByPlaceholder("Message Guru").fill("Looking into your upload now.");
  await admin.getByRole("button", { name: "Send" }).click();
  await (await sent).finished();
  await expect(banner).toBeVisible();
  await expectAccessible(admin);

  // The way back.
  await banner.getByRole("button", { name: "Stop viewing" }).click();
  await expect(banner).toHaveCount(0);
  await admin.getByRole("link", { name: "Admin" }).click();
  const visit = admin.getByRole("row").filter({ hasText: "They report their upload never became a lesson" });
  await expect(visit).toBeVisible();
  await visit.getByRole("button", { name: "Action log" }).click();
  await expect(visit.getByText(/POST .*\/messages/)).toBeVisible();

  // The learner sees who wrote it, and nothing the administrator did is their evidence.
  await learner.reload();
  await expect(learner.getByText("Looking into your upload now.")).toBeVisible();
  await expect(learner.getByText("Admin", { exact: true })).toBeVisible();
  const activity = await apiGet<{ observations_last_7d: number }>(learner, "/activity");
  expect(activity.observations_last_7d).toBe(0);
});
```

Before running: confirm `/api/v1/me` returns `email` (`grep -n '"/me"' -A12 app/api/v1/*.py`). If it does not, use the email `signIn` returned for the learner instead (`const learnerAccount = await signIn(learner)`), and record the ruling.

- [ ] **Step 3: Run it — expect RED**

Run: `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test e2e/admin-visit.spec.ts --reporter=line`
Expected: FAIL at the banner — "Viewing accounts is switched off for this deployment." (the switch is off in the e2e backend).

- [ ] **Step 4: Turn the switch on for journeys**

In `scripts/e2e-backend.sh`, after the model exports:

```bash
# Administrator visits (P10) are default-off in every real deployment; the journeys turn them
# on so the visit itself can be driven. Nothing else in the journeys depends on it being off.
export GURU_IMPERSONATION_ENABLED=true
```

- [ ] **Step 5: Run — GREEN, three times**

Run: `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test e2e/admin-visit.spec.ts e2e/admin.spec.ts --repeat-each=3 --reporter=line`
Expected: 9 passed. Any other failure is a finding: fix the product (systematic-debugging), not the assertion, and record it.

- [ ] **Step 6: Commit**

```bash
git add frontend/e2e/admin-visit.spec.ts frontend/e2e/admin.ts frontend/e2e/admin.spec.ts frontend/e2e/journey.ts scripts/e2e-backend.sh
git commit -m "test(e2e): drive an administrator's audited visit to a learner's account [S58]"
```

---

### Task 4: No web addresses (v0) journey

**Files:**
- Create: `frontend/e2e/web-restriction.spec.ts`

**Interfaces:**
- Consumes: `signIn`, `API_BASE` from `e2e/journey.ts`.

- [ ] **Step 1: Write the journey**

```typescript
import { expect, test } from "@playwright/test";

import { API_BASE, signIn } from "./journey";
import { expectAccessible } from "./a11y";

/** v0 reads no web page (S30, V0_DECISIONS). The backend refusal and the dead legacy jobs are
 * pinned in `tests/test_v0_web_policy.py`; this is the half a browser can see: nothing offers a
 * web address, the old door answers 403 to a real signed-in browser, and a tutoring turn makes
 * the browser fetch nothing beyond the app and its API. Server-side fetches are not visible
 * here and are the backend suite's to rule out. */

test("the library takes files only, and the old link import is refused", async ({ page }) => {
  await signIn(page);
  await page.goto("/app/uploads");
  await page.locator('input[type="file"]').setInputFiles({
    name: `notes-${Date.now()}.txt`,
    mimeType: "text/plain",
    buffer: Buffer.from("Mitochondria produce most of the cell's ATP."),
  });
  await expect(page.getByText(/notes-\d+\.txt/)).toBeVisible();

  await expect(page.getByRole("textbox", { name: /url|link|web address/i })).toHaveCount(0);
  await expect(page.getByPlaceholder(/https?:/i)).toHaveCount(0);

  const refused = await page.request.post(`${API_BASE}/api/v1/sources/link`, {
    data: { url: "https://example.com/article" },
  });
  expect(refused.status()).toBe(403);

  await page.reload();
  await expect(page.getByText(/notes-\d+\.txt/)).toBeVisible();
  await expectAccessible(page);
});

test("a tutoring turn makes the browser reach nothing but the app and its API", async ({ page }) => {
  // The bundle's origin comes from the project's baseURL; the API's from the journey helpers.
  const allowed = new Set([new URL(API_BASE).origin, new URL(test.info().project.use.baseURL!).origin]);
  const elsewhere: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol !== "http:" && url.protocol !== "https:") return; // data:, blob:
    if (!allowed.has(url.origin)) elsewhere.push(request.url());
  });

  await signIn(page);
  await page.goto("/app/chat");
  await page.getByRole("button", { name: "New chat" }).click();
  await page.getByRole("button", { name: /General — no library grounding/ }).click();
  await page.getByRole("button", { name: "Start chat" }).click();
  await page.getByRole("button", { name: "Agentic" }).click();
  const stream = page.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await page.getByPlaceholder("Message Guru").fill("Look up https://example.com/article for me.");
  await page.getByRole("button", { name: "Send" }).click();
  await (await stream).finished();

  expect(elsewhere).toEqual([]);
});
```

Before running, confirm the link route's body shape (`sed -n 117,130p app/api/v1/sources.py`). It answers 403 regardless of body; if it validates first and answers 422 for this body, send the body it declares and record the ruling.

- [ ] **Step 2: Run three times**

Run: `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test e2e/web-restriction.spec.ts --repeat-each=3 --reporter=line`
Expected: 6 passed. These assert existing behaviour, so a first-run pass is expected; to prove the network assertion can fail, temporarily add `await page.evaluate(() => fetch("https://example.org/").catch(() => {}))` before the `expect`, see it FAIL listing `https://example.org/`, then remove it.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/web-restriction.spec.ts
git commit -m "test(e2e): v0 takes files only, refuses the link import, and the browser fetches nothing else [S58]"
```

---

### Task 5: Exact note edits journey

**Files:**
- Modify: `app/llm/providers/shaped.py` (a `note distill` shape)
- Modify: `tests/test_shaped_provider.py` (coupling + parse test)
- Modify: `frontend/src/pages/NoteView.tsx` (accessible name for the editor)
- Create: `frontend/e2e/notes.spec.ts`

**Interfaces:**
- Consumes: `seedSubjectWithPlan`, `api`, `apiGet`, `signIn` from `e2e/journey.ts`.
- Produces: shape `Shape("note distill", "You maintain a learner's personal study notes", _note_distill)`.

- [ ] **Step 1: Failing coupling test**

In `tests/test_shaped_provider.py`, import `from app.learning.note_distill import DISTILL_SYSTEM_PROMPT, parse_atoms_payload` and add `("note distill", DISTILL_SYSTEM_PROMPT)` to `REAL_PROMPTS`. Add:

```python
async def test_a_note_distill_reply_parses_into_one_concept_atom() -> None:
    reply = await _client().complete(
        ModelRole.SMART,
        [ChatMessage(role=ChatRole.USER, content="Topic: 'Eigenvalues' in the subject 'LA'\n")],
        system=DISTILL_SYSTEM_PROMPT,
    )
    payload = parse_atoms_payload(reply.content)
    assert payload is not None
    assert [a["kind"] for a in payload["atoms"]] == ["concept"]
    assert "Eigenvalues" in payload["atoms"][0]["md"]
```

Run: `uv run pytest tests/test_shaped_provider.py -q`
Expected: FAIL — the coupling test finds no shape for "note distill", and the parse test gets prose (`payload is None`).

- [ ] **Step 2: Add the shape**

In `shaped.py`, before `SHAPES`:

```python
# --- notes ------------------------------------------------------------------------------------


def _note_distill(prompt: str) -> str:
    """One concept atom naming the topic, so a note exists and can be edited (S58).

    No KC tags and no provenance: the distiller validates both against the topic and the
    evidence labels, and an empty list is always valid. Rendering is left to prose, which the
    renderer accepts as-is.
    """
    match = re.search(r"^Topic: '([^']*)'", prompt, re.M)
    topic = match.group(1) if match else "this topic"
    md = f"**{topic}** — the idea in one sentence, from your practice."
    return json.dumps({"atoms": [{"kind": "concept", "kc_ids": [], "md": md}]})
```

and add `Shape("note distill", "You maintain a learner's personal study notes", _note_distill),` to `SHAPES`.

Run: `uv run pytest tests/test_shaped_provider.py -q`
Expected: all pass.

- [ ] **Step 3: Write the journey**

```typescript
import { expect, test, type Page } from "@playwright/test";

import { API_BASE, api, apiGet, seedSubjectWithPlan, signIn } from "./journey";
import { expectAccessible } from "./a11y";

/** A learner's own wording in their notes is kept exactly (S40, S58).
 *
 * Exactness and the revision guard are pinned in the backend suite; this drives them through
 * the editor a learner actually uses — including the case where the note changes underneath an
 * open editor, which must refuse the save and keep the draft rather than lose either text.
 */

const EXACT = "My  own words,\n  kept   exactly — even the spacing.";

type NoteEntry = { topic_id: string; stale: boolean; has_note: boolean };
type Note = { revision_ordinal: number; learner_authored_md: string | null; content_md: string | null };

async function practisedNote(page: Page): Promise<string> {
  await signIn(page);
  const subjectId = await seedSubjectWithPlan(page, `Understand eigenvalues ${Date.now()}`);
  await page.goto(`/app/lessons?subject_id=${subjectId}`);
  await page.getByRole("button", { name: "Start practice" }).click();
  await expect(page.getByRole("complementary").getByText(/In your own words/)).toBeVisible();
  const stream = page.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await page.getByPlaceholder("Your answer…").fill("An eigenvalue scales its eigenvector.");
  await page.getByRole("button", { name: "Send" }).click();
  await (await stream).finished();

  const entries = await apiGet<NoteEntry[]>(page, `/subjects/${subjectId}/notes`);
  const practised = entries.find((e) => e.stale) ?? entries[0];
  const note = await api<Note>(page, "post", `/topics/${practised.topic_id}/note/refresh`, {});
  expect(note.revision_ordinal, "the stand-in distilled a first revision").toBeGreaterThan(0);
  return practised.topic_id;
}

test("a learner's edit is saved exactly and survives a reload", async ({ page }) => {
  const topicId = await practisedNote(page);
  await page.goto(`/app/notes/${topicId}`);
  await page.getByRole("button", { name: "Edit" }).click();
  const editor = page.getByRole("textbox", { name: "Your note" });
  await expectAccessible(page);
  await editor.fill(EXACT);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(editor).toHaveCount(0);

  await page.reload();
  await page.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByRole("textbox", { name: "Your note" })).toHaveValue(EXACT);
  const saved = await apiGet<Note>(page, `/topics/${topicId}/note`);
  expect(saved.learner_authored_md).toBe(EXACT);
});

test("a note that changed under an open editor refuses the save and keeps the draft", async ({
  page,
}) => {
  const topicId = await practisedNote(page);
  await page.goto(`/app/notes/${topicId}`);
  await page.getByRole("button", { name: "Edit" }).click();
  const editor = page.getByRole("textbox", { name: "Your note" });
  await editor.fill(EXACT);

  // Another tab saves first.
  const current = await apiGet<Note>(page, `/topics/${topicId}/note`);
  const other = await page.request.put(`${API_BASE}/api/v1/topics/${topicId}/note`, {
    data: { content_md: "Written in another tab.", expected_revision_ordinal: current.revision_ordinal },
  });
  expect(other.ok()).toBeTruthy();

  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText(/Reload it and reapply your changes/)).toBeVisible();
  await expect(editor).toHaveValue(EXACT);
});
```

The PUT goes to the API origin (`API_BASE`), not the page's — the bundle and the API are served from different ports.

- [ ] **Step 4: Run — expect RED on accessibility**

Run: `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test e2e/notes.spec.ts --reporter=line`
Expected: FAIL — `getByRole("textbox", { name: "Your note" })` resolves to nothing (the editor `<textarea>` has no accessible name), which axe also reports as a critical `label` violation.

- [ ] **Step 5: Name the editor**

In `NoteView.tsx`, give the textarea `aria-label="Your note"`.

- [ ] **Step 6: Run — GREEN, three times; then unit tests**

Run: `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test e2e/notes.spec.ts --repeat-each=3 --reporter=line`
Expected: 6 passed. If the conflict message differs, read `jfetch`'s error mapping in `src/api/` and fix the product if it hides the server's `detail` (the message is the learner's only instruction); record it.

Run: `npm test && npm run lint && npm run build` (from `frontend/`) and `uv run pytest tests/test_shaped_provider.py -q`.
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/llm/providers/shaped.py tests/test_shaped_provider.py frontend/src/pages/NoteView.tsx frontend/e2e/notes.spec.ts
git commit -m "test(e2e): a learner's note edit is kept exactly, and a stale save keeps the draft [S58]"
```

---

### Task 6: Required check, docs, and the whole-suite run

**Files:**
- Modify: `docs/OPERATIONS.md` ("Deploying a release")
- Modify: `docs/RUNBOOK.md` (§7 "Before you merge")
- Modify: `docs/guru-suggestions-tracker.md` (S58 row → Completed; S58 notes; counts; next up)

- [ ] **Step 1: Full local gates**

Run: `uv run poe check` (background, output to a file) · `uv run poe format-check` · from `frontend/`: `npm test && npm run lint && npm run build` · `VITE_CLERK_PUBLISHABLE_KEY= CI=1 npx playwright test --reporter=line`.
Expected: all green; the full browser suite passes with no failures (the admin journey included, on the fresh database).

- [ ] **Step 2: Docs**

RUNBOOK §7: replace "CI runs three gates: …" with the current list (as in README's Testing section) and add:

> **`main` requires the `CI` check.** A PR cannot merge until the aggregate `CI` job is green;
> administrators can still push directly (docs go straight to `main`). The browser journeys
> recreate their `_e2e` database at every backend start, so a local run begins empty like CI —
> unless Playwright reuses a backend you left running (`reuseExistingServer` outside CI); run
> with `CI=1` to be sure. Journeys run with administrator visits switched on.

OPERATIONS "Deploying a release", new final paragraph:

> `main` is protected: the `CI` check must pass before a PR merges (administrators may push
> directly). The rule names the aggregate job, so renaming `CI` in `ci.yml` silently removes the
> gate — update the rule in the same change (`gh api repos/<owner>/<repo>/branches/main/protection`).

- [ ] **Step 3: Apply the branch rule**

```bash
cat > "$SCRATCH/protection.json" <<'EOF'
{"required_status_checks":{"strict":false,"contexts":["CI"]},"enforce_admins":false,"required_pull_request_reviews":null,"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}
EOF
gh api -X PUT repos/alykapasi/guru-alt/branches/main/protection --input "$SCRATCH/protection.json" >/dev/null
gh api repos/alykapasi/guru-alt/branches/main/protection -q '{checks: .required_status_checks.contexts, strict: .required_status_checks.strict, admins: .enforce_admins.enabled, force: .allow_force_pushes.enabled, delete: .allow_deletions.enabled}'
```

Expected: `{"admins":false,"checks":["CI"],"delete":false,"force":false,"strict":false}`.

- [ ] **Step 4: Tracker**

Move S58 to Completed (closed by: three journeys — administrator visit, files-only v0 library and browser egress, exact note edits; fresh `_e2e` database per backend start; mastery test on the database clock; `main` requires `CI`, admins bypass; hand-off: S60 real infrastructure). Live v0 12 → 11, Done 53 → 54, "Next up: workstream 6 — S59, S18". S58 notes: drop the "Order-dependent failures (unfixed)" bullet; keep the rest. Record any minors from the final review under Deferred minors.

- [ ] **Step 5: Commit**

```bash
git add docs/OPERATIONS.md docs/RUNBOOK.md docs/guru-suggestions-tracker.md
git commit -m "docs: S58 — required CI on main, fresh journey database, three new journeys [S58]"
```

- [ ] **Step 6: Verify the gate on the PR** (after the final review's fixes are pushed and the PR is open)

Run: `gh pr view <N> --json mergeStateStatus -q .mergeStateStatus` while CI is running.
Expected: `BLOCKED`; after CI passes: `CLEAN`. Record both in the PR body.
