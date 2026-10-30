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
type Note = {
  revision_ordinal: number;
  learner_authored_md: string | null;
  content_md: string | null;
};

/** A subject, one graded answer, and the note that answer produced — arranged the way a
 * learner gets one: practice first, then the catch-up that distils it. */
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

  // Another tab saves first. To the API's origin: the bundle is served from a different port.
  const current = await apiGet<Note>(page, `/topics/${topicId}/note`);
  const other = await page.request.put(`${API_BASE}/api/v1/topics/${topicId}/note`, {
    data: {
      content_md: "Written in another tab.",
      expected_revision_ordinal: current.revision_ordinal,
    },
  });
  expect(other.ok()).toBeTruthy();

  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText(/Reload it and reapply your changes/)).toBeVisible();
  await expect(editor).toHaveValue(EXACT);
});
