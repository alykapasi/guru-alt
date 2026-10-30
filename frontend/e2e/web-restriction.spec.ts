import { expect, test } from "@playwright/test";

import { API_BASE, signIn } from "./journey";
import { expectAccessible } from "./a11y";

/** v0 reads no web page (S30, V0_DECISIONS). The backend refusal and the dead legacy jobs are
 * pinned in `tests/test_v0_web_policy.py`; this is the half a browser can see: nothing offers a
 * web address, the old door answers 403 to a real signed-in browser, and a tutoring turn makes
 * the browser fetch nothing beyond the app and its API. Server-side fetches are not visible
 * here and are the backend suite's to rule out. */

test("the library takes files only, and the old link import is refused", async ({ page }) => {
  const filename = `notes-${Date.now()}.txt`;
  await signIn(page);
  await page.goto("/app/uploads");
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: "text/plain",
    buffer: Buffer.from("Mitochondria produce most of the cell's ATP."),
  });
  await expect(page.getByText(filename)).toBeVisible();

  await expect(page.getByRole("textbox", { name: /url|link|web address/i })).toHaveCount(0);
  await expect(page.getByPlaceholder(/https?:/i)).toHaveCount(0);

  const refused = await page.request.post(`${API_BASE}/api/v1/sources/link`, {
    data: { url: "https://example.com/article" },
  });
  expect(refused.status()).toBe(403);

  await page.reload();
  await expect(page.getByText(filename)).toBeVisible();
  await expectAccessible(page);
});

test("a tutoring turn makes the browser reach nothing but the app and its API", async ({
  page,
}) => {
  // The bundle's origin comes from the project's baseURL; the API's from the journey helpers.
  const allowed = new Set([
    new URL(API_BASE).origin,
    new URL(test.info().project.use.baseURL!).origin,
  ]);
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
  await expect(page).toHaveURL(/\/app\/chat\/[0-9a-f-]{36}/);
  await page.getByRole("button", { name: "Agentic" }).click();
  const stream = page.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await page.getByPlaceholder("Message Guru").fill("Look up https://example.com/article for me.");
  await page.getByRole("button", { name: "Send" }).click();
  await (await stream).finished();

  expect(elsewhere).toEqual([]);
});
