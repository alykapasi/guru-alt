import { useClerk } from "@clerk/react";
import { useLogout } from "../api/auth";
import { clerkEnabled } from "./mode";

/** Sign out of everything this build has (S21).
 *
 * Two sessions exist when Clerk is on — Clerk's and Guru's — and ending only Guru's is worse
 * than not signing out at all: the next page load finds Clerk still signed in, exchanges
 * again, and puts the person straight back into the account they just left. On a shared
 * machine that is the whole point of the button, silently undone.
 *
 * Which hook this is gets chosen at module load, not per render, because `useClerk` only works
 * inside a `ClerkProvider` that a keyless build never mounts. See `./mode`.
 */
function useGuruSignOut() {
  const logout = useLogout();
  return async () => {
    await logout.mutateAsync();
  };
}

function useClerkAndGuruSignOut() {
  const logout = useLogout();
  const clerk = useClerk();
  return async () => {
    // Guru first, and `mutateAsync` settles rather than throws on a failed request, so a
    // backend that is down cannot leave Clerk's session standing.
    await logout.mutateAsync();
    await clerk.signOut();
  };
}

export const useSignOutEverywhere = clerkEnabled ? useClerkAndGuruSignOut : useGuruSignOut;
