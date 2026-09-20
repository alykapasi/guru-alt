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
export function newAccount(): { email: string } {
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  // example.com, not example.test: the API validates deliverability, and the reserved
  // special-use TLDs are refused before the address ever reaches a handler.
  return { email: `journey-${stamp}@example.com` };
}

/** Sign in as a fresh account, through the development sign-in (S21).
 *
 * Not through the UI any more: Clerk owns the sign-in form now, and its hosted UI cannot be
 * driven in a CI browser with no network. What these journeys exist to prove is the product
 * *behind* the sign-in — the event stream, the session cookie, the credentialed cross-origin
 * fetch — so they take the one door that works offline and exercise everything after it. The
 * journey that asserts a signed-out browser is sent to `/signin` still does exactly that.
 *
 * `page.request` shares the browser context's cookie jar, so the page is signed in too.
 */
export async function signIn(page: Page): Promise<{ email: string }> {
  const account = newAccount();
  const response = await page.request.post(`${API_BASE}/api/v1/auth/dev-login`, {
    data: { email: account.email },
  });
  expect(response.ok(), `dev-login → ${response.status()}`).toBeTruthy();
  // Returned because a journey may need to act on this account from outside the browser —
  // granting it admin, for one, which has no API by design.
  return account;
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
