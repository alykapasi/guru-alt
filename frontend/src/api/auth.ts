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

/** Sign-in and registration report the same shape of problem, so they share one error type. */
export class AuthFailed extends Error {}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { email: string; password: string }) => {
      const { data, error, response } = await api.POST("/api/v1/auth/login", { body });
      if (error || !data) {
        throw new AuthFailed(
          response.status === 401
            ? "That email and password don't match an account."
            : "Sign-in failed. Try again.",
        );
      }
      return data as CurrentLearner;
    },
    // Everything cached was fetched as somebody else (or as nobody). Clearing beats
    // invalidating: a stale conversation list belonging to the previous session must never
    // be rendered to the new one, even for the moment before a refetch lands.
    onSuccess: () => queryClient.clear(),
  });
}

export function useRegister() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { email: string; password: string; display_name?: string }) => {
      const { data, error, response } = await api.POST("/api/v1/auth/register", { body });
      if (error || !data) {
        throw new AuthFailed(
          response.status === 409
            ? "That email is already registered. Sign in instead."
            : response.status === 422
              ? "Check the email address, and use a password of at least 12 characters."
              : "Could not create the account. Try again.",
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
