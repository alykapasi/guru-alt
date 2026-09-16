import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { startVisit } from "./impersonation";

/** Reads for the operator's portal (P10).
 *
 * Both are polled rather than fetched once. The numbers they show are about *right now* — what
 * the bill is at, how the models are behaving — and a dashboard that quietly shows a figure
 * from when the tab was opened is worse than one that shows nothing, because it looks current.
 * A minute is slow enough to cost nothing and fast enough that somebody watching a deploy sees
 * it move. */
const POLL_MS = 60_000;

export function useSpend(hours: number) {
  return useQuery({
    queryKey: ["ops", "spend", hours],
    refetchInterval: POLL_MS,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/ops/spend", { params: { query: { hours } } });
      if (error) throw error;
      return data;
    },
  });
}

export function useLearnerRoster(hours: number) {
  return useQuery({
    queryKey: ["admin", "learners", hours],
    refetchInterval: POLL_MS,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/learners", {
        params: { query: { hours } },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** The record of who has viewed whose account (P10).
 *
 * Not polled. The other two answer "what is happening now"; this one is a log, and a log that
 * refreshes under the reader is harder to read rather than more current. It is invalidated
 * when a visit starts or ends, which is every way it changes from this browser. */
export function useImpersonations() {
  return useQuery({
    queryKey: ["admin", "impersonations"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/impersonations");
      if (error) throw error;
      return data;
    },
  });
}

export class VisitRefused extends Error {}

/** Start a read-only visit to a learner's account.
 *
 * The cache is cleared on success, not invalidated: everything in it was fetched as the
 * administrator, and a stale read of their own dashboard rendering under a banner that names
 * somebody else is precisely the confusion the banner exists to prevent. */
export function useStartVisit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { learnerId: string; reason: string }) => {
      const { data, error, response } = await api.POST("/api/v1/admin/impersonate", {
        body: { learner_id: body.learnerId, reason: body.reason },
      });
      if (error || !data) {
        throw new VisitRefused(
          response.status === 404
            ? "Viewing accounts is switched off for this deployment."
            : response.status === 422
              ? "Say why, in a sentence."
              : "Could not start viewing that account.",
        );
      }
      return data;
    },
    onSuccess: (data) => {
      startVisit({
        impersonationId: data.impersonation.id,
        learnerId: data.learner_id,
        learnerHandle: data.learner_handle,
        token: data.token,
        expiresAt: data.expires_at,
      });
      queryClient.clear();
    },
  });
}
