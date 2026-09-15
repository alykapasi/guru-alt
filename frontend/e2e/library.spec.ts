import { expect, test, type Page } from "@playwright/test";

import { register } from "./journey";

/** The fourth browser journey (S58): put a document in, and have it come back usable.
 *
 * Upload is the one path in the product where the learner hands over something of their own
 * and then waits. The request is only the first half of it — the bytes reach the object store
 * and the row commits inside the request, but everything that makes the document *usable*
 * happens in a queued job afterwards. So the seam here is wider than the others: browser →
 * multipart cross-origin POST → object store → queue → worker → extraction, chunking,
 * embedding → a row whose status the page is polling. A break anywhere in that chain looks
 * identical from the library: a file that sits on "pending".
 *
 * Nothing else covers it. The backend tests call `ingest_source` directly with an in-memory
 * store and no broker; the queue job proves delivery through a real broker but never uploads
 * anything; the component tests render `SourceList` against a fixture array. This is the only
 * gate where a real file goes through real storage and a real worker.
 *
 * It needs the journey environment to have both, which is why this journey did not exist until
 * `scripts/e2e-backend.sh` started a worker and CI started an object store beside it.
 */

const NOTES = [
  "Photosynthesis converts light energy into chemical energy stored in glucose.",
  "The light-dependent reactions occur in the thylakoid membranes of the chloroplast.",
  "The Calvin cycle fixes carbon dioxide into three-carbon sugars in the stroma.",
].join(" ");

/** Ingestion is a queued job, so the wait is for a worker to pick it up, extract, chunk and
 * embed — comfortably longer than a render, and still short enough that a stuck queue fails
 * the run rather than hanging it. */
const INGESTION_TIMEOUT = 20_000;

async function uploadNotes(page: Page, filename: string): Promise<void> {
  // The input is visually hidden behind a styled button, which is how the page is meant to
  // look; `setInputFiles` drives the real element rather than the button that clicks it.
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: "text/plain",
    buffer: Buffer.from(NOTES),
  });
}

test("an uploaded file is accepted, ingested, and listed as ready", async ({ page }) => {
  const filename = `notes-${Date.now()}.txt`;

  await register(page);
  await page.goto("/app/uploads");
  await expect(page.getByText("No materials yet.")).toBeVisible();

  await uploadNotes(page, filename);

  // Accepted immediately and shown as such. The row appearing before ingestion finishes is
  // the product being honest: the file is safely stored, and the work on it has not started.
  const row = page.getByText(filename);
  await expect(row).toBeVisible();

  // And then it actually becomes usable. This is the half a request-level assertion cannot
  // reach — it needs the queue to have delivered and a worker to have done the work.
  await expect(page.getByText("done")).toBeVisible({ timeout: INGESTION_TIMEOUT });
  await expect(page.getByText("failed")).toHaveCount(0);
});

test("an ingested document can be built into a subject, and is filed under it", async ({
  page,
}) => {
  const filename = `photosynthesis-${Date.now()}.txt`;

  await register(page);
  await page.goto("/app/uploads");
  await uploadNotes(page, filename);
  await expect(page.getByText("done")).toBeVisible({ timeout: INGESTION_TIMEOUT });

  // The wizard is the only thing uploads currently feed, so "the upload worked" and "the
  // upload is usable" are the same claim only if it can be picked here.
  await page.goto("/app/subjects/new");
  await page.getByRole("checkbox").first().check();
  await page.getByRole("button", { name: "Next" }).click();

  await page.getByPlaceholder(/Learn Python for data analysis/).fill("Understand photosynthesis");
  await page.getByRole("button", { name: "Send" }).click();
  await page.getByRole("button", { name: "Looks good" }).click();
  await expect(page.getByRole("heading", { name: "Review your curriculum" })).toBeVisible();
  const subject = await page.getByPlaceholder("Subject name").inputValue();

  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Create subject" }).click();
  await expect(page).toHaveURL(/\/app\/lessons\?subject_id=[0-9a-f-]{36}/);

  // Committing a subject with material reassigns that material to it, so the document is no
  // longer loose in the library — it belongs to the thing it was used to build. Filtering to
  // the subject is what proves the reassignment reached the database rather than only the
  // request the wizard sent.
  await page.goto("/app/uploads");
  await page.getByRole("button", { name: subject }).click();
  await expect(page.getByText(filename)).toBeVisible();
});
