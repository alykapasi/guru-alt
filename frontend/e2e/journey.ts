import { expect, type Page } from "@playwright/test";

/** Shared arrangement for the browser journeys (S58).
 *
 * Not a spec file — Playwright's default `testMatch` only collects `*.spec.ts`, so this is
 * imported rather than run.
 */

/** Where the API answers. The same value `playwright.config.ts` hands the built bundle, so a
 * journey talking to the API directly and the page talking to it agree on the origin — which
 * is what lets them share a session cookie. */
export const API_BASE = `http://localhost:${process.env.GURU_E2E_API_PORT ?? "8000"}`;

/** A fresh account per run. The journeys commit, and a fixed address would make the second
 * run of the day fail on a unique constraint with a message about email addresses. */
export function newAccount(): { email: string; password: string } {
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  // example.com, not example.test: the API validates deliverability, and the reserved
  // special-use TLDs are refused before the address ever reaches a handler.
  return { email: `journey-${stamp}@example.com`, password: "journey-password-1" };
}

/** Register through the form and land signed in.
 *
 * Through the form rather than a development sign-in for two reasons: the dev-login button is
 * compiled out of the production build these run against on purpose, and registering is the
 * path a first user actually takes.
 */
export async function register(page: Page): Promise<void> {
  const account = newAccount();
  await page.goto("/signin");
  await page.getByRole("button", { name: "Create one" }).click();
  await page.getByLabel("Email").fill(account.email);
  // Not an exact match: in register mode the field's label carries the "At least 12
  // characters" hint, so its accessible name is the two of them together.
  await page.getByLabel(/^Password/).fill(account.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/app\/chat/);
}

/** One API call as the signed-in learner.
 *
 * `page.request` shares the browser context's cookie jar, so this is the same session the page
 * is using — no second sign-in, and no token handed around out of band.
 */
export async function api<T>(page: Page, method: "post", path: string, body: unknown): Promise<T> {
  const response = await page.request[method](`${API_BASE}/api/v1${path}`, { data: body });
  expect(response.ok(), `${method.toUpperCase()} ${path} → ${response.status()}`).toBeTruthy();
  return (await response.json()) as T;
}

/** A committed subject with a lesson plan, arranged over the API rather than through the UI.
 *
 * Deliberate: the wizard has its own journey, and repeating all four of its steps here would
 * mean a wizard regression failed two journeys and told you nothing extra about practice. What
 * this sets up is the *state* practice needs; what the practice journey drives is practice.
 */
export async function seedSubjectWithPlan(page: Page, goal: string): Promise<string> {
  const proposal = await api<unknown>(page, "post", "/onboarding/curriculum", {
    goal,
    source_ids: null,
  });
  const subject = await api<{ id: string }>(page, "post", "/subjects/commit", proposal);
  await api(page, "post", `/subjects/${subject.id}/lesson-plan`, { goal });
  return subject.id;
}
