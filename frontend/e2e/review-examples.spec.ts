import { expect, test, type Browser, type Page } from "@playwright/test";

import { grantAdmin, markEligible } from "./admin";
import { seedSubjectWithPlan, signIn } from "./journey";
import { expectAccessible } from "./a11y";

/** The founder labels their own example blind, then sees what the model decided (S59).
 *
 * Drawing, freezing and the blind route are pinned in the backend suite; this is the whole loop
 * as a person meets it — an eligible account's graded answer becomes an example the portal
 * serves without its decision, and saving a label is what reveals it.
 */

async function newPage(browser: Browser): Promise<Page> {
  return (await browser.newContext()).newPage();
}

test("an eligible learner's graded answer is labelled blind in the portal", async ({ browser }) => {
  test.slow(); // two `uv run` shell-outs

  const learner = await newPage(browser);
  const account = await signIn(learner);
  markEligible(account.email);
  const subjectId = await seedSubjectWithPlan(learner, `Understand eigenvalues ${Date.now()}`);
  await learner.goto(`/app/lessons?subject_id=${subjectId}`);
  await learner.getByRole("button", { name: "Start practice" }).click();
  await expect(learner.getByRole("complementary").getByText(/In your own words/)).toBeVisible();
  const stream = learner.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await learner.getByPlaceholder("Your answer…").fill("An eigenvalue scales its eigenvector.");
  await learner.getByRole("button", { name: "Send" }).click();
  await (await stream).finished();

  const admin = await newPage(browser);
  const founder = await signIn(admin);
  grantAdmin(founder.email);
  await admin.goto("/app/chat");
  await admin.getByRole("link", { name: "Admin" }).click();

  await admin.getByRole("button", { name: /^Grading/ }).click();
  await admin.getByRole("button", { name: "Draw 10" }).click();
  await expect(admin.getByText(/^Drew \d+\.$/)).toBeVisible();
  // Earlier runs' examples sit in the labelled list below; this is the one being labelled.
  const current = admin.getByRole("region", { name: "Next example" });
  await expect(current.getByText("An eigenvalue scales its eigenvector.")).toBeVisible();
  await expect(current.getByText(/Model decided/)).toHaveCount(0);

  await current.getByRole("radio", { name: "pass" }).check();
  await current.getByRole("button", { name: "Save label" }).click();
  await expect(current.getByText(/^Model decided: /)).toBeVisible();
  await expect(current.getByRole("button", { name: "Change label" })).toBeVisible();
  await expectAccessible(admin);
});
