/** Which sign-in this build has (S21).
 *
 * Decided once, at module load, and never re-derived. `ClerkProvider` throws when it has no
 * publishable key, and Clerk's hooks only work inside that provider — so a build with no key
 * (vitest, CI, the Playwright journeys, a checkout with nobody's Clerk account) must not render
 * the provider at all and must not call a Clerk hook at all. Deciding here rather than per
 * render is what keeps every hook call in this codebase unconditional, which is the rule React
 * actually enforces: a component that called `useAuth()` only when a key existed would break the
 * hook order the moment that changed.
 *
 * The two modes are honest about themselves. With a key, Clerk owns sign-in. Without one, the
 * app still builds, still runs, and still signs in through the development endpoint where the
 * backend offers it — which is exactly what CI needs and what a contributor with no Clerk
 * account gets.
 */
export const CLERK_PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as
  string | undefined;

export const clerkEnabled = Boolean(CLERK_PUBLISHABLE_KEY);
