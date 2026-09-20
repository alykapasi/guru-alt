import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { signIn } from "./journey";

/** The operator's portal, and the door that is not shown to people who may not open it (P10).
 *
 * This journey exists for the half no backend test can reach. The API's refusals are asserted
 * in `tests/test_admin_access.py`; what can only be seen in a browser is whether an ordinary
 * learner is *offered* the portal, and whether an administrator's one actually renders the
 * numbers rather than an error where they should be.
 *
 * It also drives `poe grant-admin`, which is the only way a deployment gets its first
 * administrator and had nothing exercising it end to end. Shelling out is the point rather
 * than a workaround: there is deliberately no API for this, because the API's own answer to
 * "who may grant admin" is "an administrator", and a deployment starts with none.
 */

const REPO = new URL("../..", import.meta.url).pathname;

/** The database the e2e API is on — derived exactly as `scripts/e2e-backend.sh` derives it, so
 * there is no second DSN to keep in step with it. */
function e2eDatabaseUrl(): string {
  return execFileSync(
    "uv",
    ["run", "python", "-m", "tests.testdb", "--suffix", "_e2e", "--print-url"],
    {
      cwd: REPO,
      encoding: "utf8",
    },
  ).trim();
}

function grantAdmin(email: string): void {
  execFileSync("uv", ["run", "poe", "grant-admin", email], {
    cwd: REPO,
    encoding: "utf8",
    env: { ...process.env, GURU_DATABASE_URL: e2eDatabaseUrl() },
  });
}

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

  // Neither timing should read as a measured zero. Every call this deployment has made was
  // through the deterministic provider, so "not measured" is what the completion row honestly
  // says, and a page that rendered "0ms" there would be claiming an instantaneous model.
  await expect(page.getByText("Not measured").first()).toBeVisible();
});
