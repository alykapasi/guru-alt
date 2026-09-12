import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useCurrentLearner } from "../api/auth";

/** Gate for every route that needs a learner (S21).
 *
 * Client-side only, and deliberately not the security boundary — the API refuses an
 * unauthenticated request whatever this component renders. What it buys is that a signed-out
 * browser sees the sign-in page instead of a shell full of failed requests, and that where
 * they were going survives the detour. */
export function RequireLearner() {
  const location = useLocation();
  const { data: learner, isPending } = useCurrentLearner();

  if (isPending) {
    return (
      <div className="bg-base-100 text-base-content/70 text-body flex min-h-svh items-center justify-center">
        Loading…
      </div>
    );
  }
  if (!learner) {
    return <Navigate to="/signin" replace state={{ from: location.pathname + location.search }} />;
  }
  return <Outlet />;
}
