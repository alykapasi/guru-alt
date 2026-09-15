import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

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
