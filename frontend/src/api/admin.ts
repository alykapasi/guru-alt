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

/** Start an audited administrator visit to a learner's account.
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

export function useAdminActions(visitId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["admin", "actions", visitId],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET(
        "/api/v1/admin/impersonations/{impersonation_id}/actions",
        {
          params: { path: { impersonation_id: visitId } },
        },
      );
      if (error) throw error;
      return data;
    },
  });
}

/** A refused account action, carrying the server's own sentence (S21).
 *
 * The server's `detail` rather than a status-derived guess, because these refusals are
 * specific and actionable — "that address is already enrolled", "that invitation was already
 * revoked", "an administrator cannot suspend themselves" — and replacing them with "Could not
 * invite" throws away the only part the operator can act on. */
export class AccountActionRefused extends Error {}

function refusalFrom(error: unknown, fallback: string): AccountActionRefused {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return new AccountActionRefused(typeof detail === "string" && detail.trim() ? detail : fallback);
}

const INVITATIONS_KEY = ["admin", "invitations"] as const;

/** Every invitation ever issued, open or spent (S21).
 *
 * Not only the open ones: the question an operator actually has is "did this person get in,
 * and if not why", and an accepted or revoked row is the answer. Not polled — invitations
 * change when somebody in this page changes them, or when an invitee accepts, and a list that
 * reorders under the reader is harder to read rather than more current. */
export function useInvitations() {
  return useQuery({
    queryKey: INVITATIONS_KEY,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/invitations");
      if (error) throw error;
      return data;
    },
  });
}

export function useInvite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (email: string) => {
      const { data, error } = await api.POST("/api/v1/admin/invitations", { body: { email } });
      if (error || !data) throw refusalFrom(error, "Could not send that invitation.");
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: INVITATIONS_KEY }),
  });
}

export function useRevokeInvitation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (invitationId: string) => {
      const { data, error } = await api.POST("/api/v1/admin/invitations/{invitation_id}/revoke", {
        params: { path: { invitation_id: invitationId } },
      });
      if (error || !data) throw refusalFrom(error, "Could not revoke that invitation.");
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: INVITATIONS_KEY }),
  });
}

/** Suspension and reinstatement both invalidate the roster, which is where they are visible.
 *
 * Suspending ends the account's live sessions server-side, so the roster's own view of who is
 * here is stale the moment this returns. */
export function useSuspend() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { learnerId: string; reason: string }) => {
      const { data, error } = await api.POST("/api/v1/admin/learners/{learner_id}/suspend", {
        params: { path: { learner_id: body.learnerId } },
        body: { reason: body.reason },
      });
      if (error || !data) throw refusalFrom(error, "Could not suspend that account.");
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "learners"] }),
  });
}

export function useReinstate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { learnerId: string; reason?: string }) => {
      const { data, error } = await api.POST("/api/v1/admin/learners/{learner_id}/reinstate", {
        params: { path: { learner_id: body.learnerId } },
        body: { reason: body.reason ?? null },
      });
      if (error || !data) throw refusalFrom(error, "Could not reinstate that account.");
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "learners"] }),
  });
}
