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
  const reason = `They report their upload never became a lesson ${Date.now()}`;

  // The learner, with a conversation of their own.
  const learner = await newPage(browser);
  const learnerAccount = await signIn(learner);
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

  const row = admin.getByRole("row").filter({ hasText: learnerAccount.email });
  await row.getByRole("button", { name: "View as" }).click();

  // No reason, no visit.
  await admin.getByRole("button", { name: "Start viewing" }).click();
  await expect(admin.getByText("Say why, in a sentence.")).toBeVisible();
  await admin.getByLabel("Why are you looking?").fill(reason);
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
  await expect(admin).toHaveURL(/\/app\/admin$/);
  await expect(admin.getByRole("link", { name: "Admin" })).toBeVisible();
  const visit = admin.getByRole("row").filter({ hasText: reason });
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
