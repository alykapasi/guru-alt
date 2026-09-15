import { expect, test, type Page } from "@playwright/test";

import { register } from "./journey";

/** The second browser journey (S58): turn a sentence into a subject you own.
 *
 * Four steps, four different failure modes, none of them visible to the suites on either side.
 * The wizard's own component tests render each step against a mocked mutation, so they cannot
 * see the goal never reaching the model, the proposal arriving in a shape the review step
 * cannot edit, or the commit posting a payload the API rejects. The backend tests call
 * `generate_curriculum` and `create_subject_with_graph` directly and never serialise either
 * across the wire. What the learner actually does — type a goal, agree with what comes back,
 * and end up with a subject on the lessons page — passes through both of those gaps.
 *
 * The thread through it is the *agreed* goal rather than the typed one, and that distinction
 * came out of writing this: the first version asserted that the subject would be named after
 * the sentence the learner typed, and it is not. "Looks good" accepts the gate's refined
 * proposal, and that is what curriculum generation is given. The typed sentence is an opening
 * position in a negotiation, which is the whole reason the gate is there. So the name is read
 * off the review step and followed through commit to the lessons page, and what the learner
 * controls is covered by the second test, where an edit in review has to be the thing created.
 *
 * Both tests depend on the stand-in answering a curriculum request with a real curriculum
 * derived from the request — see `app/llm/providers/shaped.py`. Against the plain fake this
 * whole journey ends at "Failed to generate curriculum". */

/** The four components `ShapedProvider` proposes, in the order it proposes them. Restated here
 * rather than imported: the spec runs under the Playwright runner with no route into the Python
 * package, and pinning them means a change to the double's curriculum has to be made here too,
 * where the assertions that depend on it live. */
const PROPOSED_KCS = ["Core vocabulary", "First principles", "Worked examples", "Common mistakes"];

async function openWizardAtTheGoalStep(page: Page): Promise<void> {
  await page.goto("/app/subjects/new");
  await expect(page.getByRole("heading", { name: "Add learning materials" })).toBeVisible();
  // Straight past materials: an empty library is the state a first user is in, and grounding
  // the curriculum in an upload is the upload journey's business, not this one's.
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByRole("heading", { name: "What's your learning goal?" })).toBeVisible();
}

test("a goal becomes a curriculum, and the curriculum becomes a subject", async ({ page }) => {
  const goal = `Understand eigenvalues ${Date.now()}`;

  await register(page);
  await openWizardAtTheGoalStep(page);

  // The refinement gate. Its reply is prose, which is the correct shape for it — the gate
  // negotiates in sentences — so this is the one model call in the wizard that the plain fake
  // could already serve.
  await page.getByPlaceholder(/Learn Python for data analysis/).fill(goal);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Hello from the fake tutor.")).toBeVisible();

  await page.getByRole("button", { name: "Looks good" }).click();

  // Generation is a single blocking call behind a spinner, so the wait is for the step to
  // change rather than for a stream to end.
  await expect(page.getByRole("heading", { name: "Review your curriculum" })).toBeVisible();

  // What flows on is the *agreed* goal — the gate's refined proposal, which is what "Looks
  // good" accepts — not the sentence the learner typed. The first version of this test asserted
  // the typed goal and failed, which is worth recording rather than quietly correcting: the
  // wizard's whole point is that the learner and the model negotiate a goal first, so the
  // typed text is an opening position, not the thing the curriculum is built from.
  //
  // So the name is read off the page rather than predicted, and the assertion is that the same
  // value survives generation, commit and display. That still distinguishes a wizard that
  // carried the agreed goal through from one that generated against anything at all.
  const agreed = await page.getByPlaceholder("Subject name").inputValue();
  expect(agreed.length).toBeGreaterThan(0);

  // Every proposed component, in order, in an editable field — read as a list rather than
  // checked one at a time, so a curriculum that arrived with three of the four, or with them
  // reordered by prerequisite resolution, fails here instead of passing a loop of existence
  // checks that never compares the whole shape.
  const proposed = await page
    .getByPlaceholder("KC name")
    .evaluateAll((fields) => fields.map((field) => (field as HTMLInputElement).value));
  expect(proposed).toEqual(PROPOSED_KCS);

  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByRole("heading", { name: "Create your subject" })).toBeVisible();
  await page.getByRole("button", { name: "Create subject" }).click();

  // Committed: the wizard hands off to the lessons page with the new subject selected.
  await expect(page).toHaveURL(/\/app\/lessons\?subject_id=[0-9a-f-]{36}/);
  await expect(page.getByRole("heading", { name: "Lessons" })).toBeVisible();
  await expect(page.getByText(agreed).first()).toBeVisible();
});

test("the curriculum the learner edits is the curriculum that gets created", async ({ page }) => {
  const goal = `Understand eigenvalues ${Date.now()}`;
  const renamed = `Linear algebra, my way ${Date.now()}`;

  await register(page);
  await openWizardAtTheGoalStep(page);
  await page.getByPlaceholder(/Learn Python for data analysis/).fill(goal);
  await page.getByRole("button", { name: "Send" }).click();
  await page.getByRole("button", { name: "Looks good" }).click();
  await expect(page.getByRole("heading", { name: "Review your curriculum" })).toBeVisible();

  // The review step exists so the learner can disagree with the model. An edit that is
  // displayed and then discarded at commit is worse than no review step at all — it asks for
  // consent to one thing and creates another.
  await page.getByPlaceholder("Subject name").fill(renamed);
  await page.getByPlaceholder("KC name").first().fill("Vocabulary, in my words");

  await page.getByRole("button", { name: "Next" }).click();
  // The summary card reads from the same state the commit posts, so the rename showing here is
  // the first evidence it survived the step boundary.
  await expect(page.getByText(renamed)).toBeVisible();
  await page.getByRole("button", { name: "Create subject" }).click();

  await expect(page).toHaveURL(/\/app\/lessons\?subject_id=[0-9a-f-]{36}/);
  await expect(page.getByText(renamed).first()).toBeVisible();
  await expect(page.getByText(goal)).toHaveCount(0);
});
