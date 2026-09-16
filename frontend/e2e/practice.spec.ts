import { expect, test, type Page } from "@playwright/test";

import { register, seedSubjectWithPlan } from "./journey";

/** The third browser journey (S58): answer a question and see what it did to you.
 *
 * This is the loop the product is for, and the one with the most machinery behind it — a
 * workflow graph that generates an item, pauses for the answer, grades it against a rubric,
 * moves the ability estimate, and reports all of that back through the same event stream the
 * chat uses. The backend tests drive `run_workflow_turn` directly; the component tests render
 * `CheckResultCard` against a hand-built result object. Neither one can see the grade failing
 * to cross the wire, and a learner whose answer silently changes nothing on screen has no way
 * to tell a working system from a broken one.
 *
 * The subject and its plan are arranged over the API (see `seedSubjectWithPlan`): the wizard
 * has its own journey, and repeating it here would mean a wizard regression failed two
 * journeys while telling you nothing extra about practice.
 *
 * What is asserted is the evidence, not the prose. The tutor's reply is the same canned
 * sentence either way — it is the grade, the reason and the movement that differ, and those
 * are the things a learner would be owed and would not get if this seam broke. */

/** Long enough to count as an attempt, and short enough to read. The stand-in behind these
 * journeys scores on length alone (`app/llm/providers/shaped.py`) — crude on purpose, so a
 * journey can drive both sides of the pass/fail branch without pretending to grade. */
const A_REAL_ATTEMPT = "An eigenvalue is the factor by which its eigenvector is scaled.";
const BARELY_AN_ANSWER = "no";

async function startPractice(page: Page, goal: string): Promise<void> {
  await register(page);
  const subjectId = await seedSubjectWithPlan(page, goal);

  await page.goto(`/app/lessons?subject_id=${subjectId}`);
  await page.getByRole("button", { name: "Start practice" }).click();
  await expect(page).toHaveURL(/\/app\/lessons\/session\/[0-9a-f-]{36}/);
}

/** Send one answer and wait for the turn to be *finished*, not merely on screen.
 *
 * The same lesson the chat journey paid for: the reply visible mid-stream is the live buffer,
 * and everything persisted — the grade included — lands only after the last frame. Asserting
 * before that is how a journey races its own subject.
 */
async function answer(page: Page, text: string): Promise<void> {
  const stream = page.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await page.getByPlaceholder("Your answer…").fill(text);
  await page.getByRole("button", { name: "Send" }).click();
  await (await stream).finished();
}

test("a practice session asks a question about the component the plan is on", async ({ page }) => {
  await startPractice(page, `Understand eigenvalues ${Date.now()}`);

  // The session sends its own opening turn on mount, so there is nothing to click: the
  // question either arrives or the panel sits on its placeholder forever.
  const panel = page.getByRole("complementary");
  await expect(panel.getByText("Preparing your practice…")).toHaveCount(0);
  // Generated from the knowledge component the plan made active, not picked from a fixture —
  // which is the difference between a question and a question *for this learner*.
  await expect(panel.getByText(/In your own words, explain/)).toBeVisible();
  // Exact, because the component's name also appears inside the question it generated — and a
  // substring match resolves to both, which `toBeVisible` reports as a strict-mode violation
  // rather than retrying past.
  await expect(panel.getByText("Core vocabulary", { exact: true })).toBeVisible();
});

test("a poor answer is marked, explained, and moves the estimate down", async ({ page }) => {
  await startPractice(page, `Understand eigenvalues ${Date.now()}`);
  await expect(page.getByRole("complementary").getByText(/In your own words/)).toBeVisible();

  await answer(page, BARELY_AN_ANSWER);

  // The record of the grade, in the learner's own view. Until the tenth pass none of this left
  // the server: the answer moved the ability estimate, rescheduled the card and revised the
  // plan, and the tutor's next paragraph was the only evidence any of it had happened.
  const grade = page.getByRole("region", { name: "How your answer was graded" });
  await expect(grade).toBeVisible();
  await expect(grade.getByText("Marked — not quite yet")).toBeVisible();
  // Why it fell short, in the learner's terms rather than the grader's vocabulary (S09). This
  // is also the assertion that caught the stand-in returning a diagnosis the product
  // deliberately drops — `incomplete` names no specific failure, so it rendered as no reason
  // at all and this journey would have had nothing to check.
  await expect(grade.getByText("The idea itself needs another look")).toBeVisible();
  // Movement, shown as a direction rather than asserted as a number: the estimator's exact
  // step is its own business and pinning it here would make this journey fail on a tuning
  // change that broke nothing.
  await expect(grade.getByText(/%\s*→\s*\d+%|unchanged at/)).toBeVisible();
});

test("a real attempt is marked correct and ends the round", async ({ page }) => {
  await startPractice(page, `Understand eigenvalues ${Date.now()}`);
  await expect(page.getByRole("complementary").getByText(/In your own words/)).toBeVisible();

  await answer(page, A_REAL_ATTEMPT);

  const grade = page.getByRole("region", { name: "How your answer was graded" });
  await expect(grade.getByText("Marked correct")).toBeVisible();
  // A component nothing diagnosed shows no reason rather than a plausible one — "we could not
  // tell" and "it was fine" are different things to tell somebody about their own work.
  await expect(grade.getByText("The idea itself needs another look")).toHaveCount(0);

  // The round is over, and the panel says so in the understated way the design calls for. The
  // composer being disabled is the half that matters: an answer typed into a finished round
  // would be graded against an item the workflow has already closed.
  const panel = page.getByRole("complementary");
  await expect(panel.getByText("Correct — nice work!")).toBeVisible();
  await expect(page.getByPlaceholder("Your answer…")).toBeDisabled();
  await expect(panel.getByRole("link", { name: "Back to lesson plan" })).toBeVisible();
});

test("the grade survives a reload, because it is stored on the message that reported it", async ({
  page,
}) => {
  await startPractice(page, `Understand eigenvalues ${Date.now()}`);
  await expect(page.getByRole("complementary").getByText(/In your own words/)).toBeVisible();
  await answer(page, BARELY_AN_ANSWER);
  await expect(page.getByRole("region", { name: "How your answer was graded" })).toBeVisible();

  // A learner who wants to argue with yesterday's grade needs the grade itself to point at.
  // Before the eleventh pass this evidence lived in the stream and died with it.
  const session = page.url();
  await page.reload();

  await expect(page).toHaveURL(session);
  const grade = page.getByRole("region", { name: "How your answer was graded" });
  await expect(grade).toHaveCount(1);
  await expect(grade.getByText("The idea itself needs another look")).toBeVisible();
});
