import { useQueryClient } from "@tanstack/react-query";
import { Eye } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { endVisit, useVisit } from "../api/impersonation";

/** What stops an administrator forgetting whose account they are looking at (P10).
 *
 * The classic failure of impersonation is not the access, it is losing track of it: somebody
 * opens a visit to diagnose an upload, wanders off, and reads the next twenty minutes of a
 * stranger's account believing it is their own. So this is unmissable rather than tasteful —
 * it sits above the nav on every page of the app, names the account, and carries the way out.
 *
 * It names administrator access and recorded actions so the actual actor stays visible.
 */
export function ImpersonationBanner() {
  const visit = useVisit();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  if (!visit) return null;

  async function stop() {
    if (!visit) return;
    // Drop the token *before* the request, not after. The route takes an administrator and
    // refuses a visit, so a DELETE carrying the visit's bearer 403s and ends nothing — while this
    // browser forgets the token anyway and looks as if it worked. It is not `/auth/logout` for
    // the same reason in the other direction: that would clear the session cookie, which is
    // the administrator's own.
    const { impersonationId } = visit;
    endVisit();
    // Off the learner's pages and onto fresh reads before the request, not after it: the page
    // on screen is the learner's, and with the banner gone it reads as the administrator's own.
    // Reset rather than clear — clearing empties the cache but leaves mounted pages showing
    // what they last read; resetting refetches them as the administrator. Back to the portal,
    // because the learner's URLs mean nothing to the administrator's account (S58).
    navigate("/app/admin");
    void queryClient.resetQueries();
    try {
      await api.DELETE("/api/v1/admin/impersonations/{impersonation_id}", {
        params: { path: { impersonation_id: impersonationId } },
      });
    } catch {
      // The visit expires on its own (`expires_at`); this browser has already dropped its token.
    }
  }

  return (
    <div
      role="status"
      className="bg-warning text-warning-content flex flex-wrap items-center justify-center gap-3 px-6 py-2 text-caption"
    >
      <Eye size={16} aria-hidden />
      <span>
        Admin access to <strong>{visit.learnerHandle}</strong>&rsquo;s account. Actions are recorded
        against your administrator identity.
      </span>
      <button type="button" className="btn btn-xs" onClick={stop}>
        Stop viewing
      </button>
    </div>
  );
}
