import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

/** The learner this browser is signed in as, or null (S21).
 *
 * A 401 is the *answer* here, not a failure: it is how the app learns nobody is signed in.
 * So it resolves to null rather than throwing, retries are off (retrying a 401 changes
 * nothing and delays the login screen), and the result is cached so every page does not ask
 * again. `queryKey` is shared with the mutations below, which invalidate it. */
export const ME_KEY = ["auth", "me"] as const;

export type CurrentLearner = {
  id: string;
  handle: string;
  display_name: string | null;
  email: string | null;
  /** Whether to offer the operator's portal at all (P10). Not what authorizes it — the API
   * refuses a non-administrator whatever the browser renders. */
  is_admin: boolean;
};

export function useCurrentLearner() {
  return useQuery({
    queryKey: ME_KEY,
    retry: false,
    staleTime: 5 * 60 * 1000,
    queryFn: async (): Promise<CurrentLearner | null> => {
      const { data, error, response } = await api.GET("/api/v1/auth/me");
      if (response.status === 401) return null;
      if (error) throw error;
      return data as CurrentLearner;
    },
  });
}

/** A refused exchange, carrying the status because the caller must act on it (S21).
 *
 * 403 and 502 mean opposite things here and the page treats them oppositely: a 403 is Guru
 * saying this person may not come in, so Clerk's session should end too; a 502 is Guru unable
 * to reach Clerk to ask, which says nothing about the person and must not sign them out.
 */
export class ExchangeFailed extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** The server's own explanation, when it sent one.
 *
 * Read from the parsed error rather than the `Response`: openapi-fetch has already consumed
 * the body by the time we see it, so re-reading it yields nothing and every refusal would
 * arrive wearing the same generic fallback.
 */
function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" && detail.trim() ? detail : fallback;
}

/** Trade the Clerk session token for Guru's own session (S21).
 *
 * Once, at sign-in — not per request. Everything downstream (the session cookie, admin visits,
 * SSE, every existing test) keeps working unchanged because what it gets back is the same
 * `guru_session` cookie it always had.
 */
export function useExchange() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (token: string) => {
      const { data, error, response } = await api.POST("/api/v1/auth/exchange", {
        params: { header: { authorization: `Bearer ${token}` } },
      });
      if (error || !data) {
        throw new ExchangeFailed(
          response.status,
          detailOf(
            error,
            response.status >= 500
              ? "Could not reach the sign-in service. Try again in a moment."
              : "Could not sign you in.",
          ),
        );
      }
      return data as CurrentLearner;
    },
    onSuccess: () => queryClient.clear(),
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await api.POST("/api/v1/auth/logout");
    },
    // Runs whether or not the request succeeded: the session may already be gone, and the one
    // thing that must not happen is a signed-out browser still showing the last learner's work.
    onSettled: () => queryClient.clear(),
  });
}

/** Sign in as the development learner, where the backend still offers that (S21).
 *
 * Returns false when the endpoint is not there — which is what production looks like — so the
 * button can simply not be shown rather than the page handling an error. */
export function useDevLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, response } = await api.POST("/api/v1/auth/dev-login");
      if (response.status === 404) return null;
      return (data as CurrentLearner) ?? null;
    },
    onSuccess: () => queryClient.clear(),
  });
}
