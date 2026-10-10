import { expect, test } from "@playwright/test";
import { grantAdmin } from "./admin";
import { apiGet, signIn } from "./journey";
import { expectAccessible } from "./a11y";

/** The operator's portal, and the door that is not shown to people who may not open it (P10).
 *
 * This journey exists for the half no backend test can reach. The API's refusals are asserted
 * in `tests/test_admin_access.py`; what can only be seen in a browser is whether an ordinary
 * learner is *offered* the portal, and whether an administrator's one actually renders the
 * numbers rather than an error where they should be.
 *
 * It also drives `poe grant-admin` (see `./admin.ts`), which is the only way a deployment gets
 * its first administrator.
 */

test("an ordinary learner is not shown the portal, and cannot reach it by URL", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/app/chat");

  await expect(page.getByRole("link", { name: "Admin" })).toHaveCount(0);

  // Following the URL out of curiosity lands somewhere useful rather than on a refusal page:
  // there is nothing they can do about it, and a "forbidden" screen invites a hunt for a way.
  await page.goto("/app/admin");
  await expect(page).not.toHaveURL(/\/app\/admin/);
});

test("an administrator reads what the deployment costs and who is on it", async ({ page }) => {
  test.slow(); // two `uv run` invocations before the browser does anything
  const account = await signIn(page);
  await page.goto("/app/chat");

  grantAdmin(account.email);
  await page.reload();

  await page.getByRole("link", { name: "Admin" }).click();
  await expect(page).toHaveURL(/\/app\/admin/);

  await expect(page.getByRole("heading", { name: "What this deployment is doing" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "By role and model" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Who is here" })).toBeVisible();

  // The roster is ordered by cost and capped, so on a database with hundreds of throwaway
  // journey accounts this learner sorts below the cut. That the page *says so* is the
  // assertion — it is what this journey found missing the first time it ran, when a hundred
  // rows out of 188 looked exactly like all of them.
  const roster = page.getByRole("heading", { name: "Who is here" }).locator("..");
  await expect(roster.getByRole("row").first()).toBeVisible();

  // Each timing says what was measured: "Not measured" exactly when no call in the window was
  // timed, and a p50/p95 otherwise — never a measured zero standing in for no data. Read from
  // the deployment's own totals rather than assumed, because whether any call was timed
  // depends on which journeys ran before this one in the same run (S58).
  const spend = await apiGet<{ completion: { calls: number }; first_token: { calls: number } }>(
    page,
    "/ops/spend?hours=24",
  );
  for (const [label, timing] of [
    ["Completion", spend.completion],
    ["Time to first token", spend.first_token],
  ] as const) {
    const block = page.getByText(label, { exact: true }).locator("..");
    if (timing.calls === 0) {
      await expect(block.getByText("Not measured")).toBeVisible();
    } else {
      await expect(block.getByText(/p50 .* · p95 /)).toBeVisible();
      await expect(block.getByText("Not measured")).toHaveCount(0);
    }
  }
  await expectAccessible(page);
});
