import { Navigate, Outlet } from "react-router-dom";
import { useCurrentLearner } from "../api/auth";

/** Gate for the operator's portal (P10).
 *
 * Client-side only, and deliberately not the security boundary — the API answers 403 to a
 * learner who is not an administrator whatever this renders. What it buys is that somebody who
 * follows the URL out of curiosity lands somewhere useful instead of on a page of failed
 * requests, and that a signed-in non-administrator is not shown a door.
 *
 * Redirects rather than rendering "forbidden": there is nothing they can do about it, and a
 * refusal page invites them to go and find a way. */
export function RequireAdmin() {
  const { data: learner, isPending } = useCurrentLearner();

  if (isPending) {
    return <p className="text-caption text-base-content/50 px-6 py-8">Loading…</p>;
  }
  if (!learner?.is_admin) {
    return <Navigate to="/app" replace />;
  }
  return <Outlet />;
}
