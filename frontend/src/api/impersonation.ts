import { useSyncExternalStore } from "react";

/** The credential for an administrator's read-only visit to a learner's account (P10).
 *
 * **In memory only, deliberately.** Putting it in `sessionStorage` would survive a reload and
 * would also be readable by any script on the page — and the app's own session is an httpOnly
 * cookie precisely so that it is not. Trading that property away to save a support engineer
 * one click, for a credential that expires in fifteen minutes and cannot write anything, is
 * the wrong side of the deal. A reload ends the view; the visit itself expires on its own.
 *
 * A module-level store rather than React context because `client.ts` has to read it from
 * outside the component tree — every request carries it, including the ones a hook makes.
 */

export type Visit = {
  impersonationId: string;
  learnerId: string;
  learnerHandle: string;
  token: string;
  expiresAt: string;
};

let current: Visit | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

export function startVisit(visit: Visit): void {
  current = visit;
  emit();
}

export function endVisit(): void {
  current = null;
  emit();
}

/** The token every request should carry, or null. Read by `client.ts` on each call. */
export function visitToken(): string | null {
  return current?.token ?? null;
}

export function currentVisit(): Visit | null {
  return current;
}

/** Subscribe a component to the visit — the banner, and anything that must not act as if the
 * signed-in learner were the one being viewed. */
export function useVisit(): Visit | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    currentVisit,
    () => null,
  );
}
