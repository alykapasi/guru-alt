import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { components } from "./schema";

/** The founder's review tab (S59): counts per area, drawing, the next example served blind, and
 * saving a label — after which the server reveals what the model decided. */

export type EvalArea = components["schemas"]["EvalAreaCounts"];
export type BlindExample = components["schemas"]["EvalExampleBlind"];
export type RevealedExample = components["schemas"]["EvalExampleRevealed"];

export function useEvalAreas() {
  return useQuery({
    queryKey: ["admin", "eval", "areas"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/eval/areas");
      if (error || !data) throw new Error("Could not read the evaluation areas.");
      return data;
    },
  });
}

export function useNextExample(area: string | null, skip: string[]) {
  return useQuery({
    queryKey: ["admin", "eval", "next", area, skip],
    enabled: area !== null,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/eval/{area}/next", {
        params: { path: { area: area! }, query: skip.length ? { skip } : {} },
      });
      if (error) throw new Error("Could not read the next example.");
      return data ?? null;
    },
  });
}

export function useLabelled(area: string) {
  return useQuery({
    queryKey: ["admin", "eval", "labelled", area],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/eval/{area}/labelled", {
        params: { path: { area } },
      });
      if (error || !data) throw new Error("Could not read the labelled examples.");
      return data;
    },
  });
}

export function useDraw(area: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/v1/admin/eval/{area}/draw", {
        params: { path: { area } },
        body: { limit: 10 },
      });
      if (error || !data) throw new Error("Could not draw examples.");
      return data.drawn;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "eval"] }),
  });
}

export function useSaveLabel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { id: string; label: Record<string, string>; note: string }) => {
      const { data, error } = await api.PUT("/api/v1/admin/eval/examples/{example_id}/label", {
        params: { path: { example_id: body.id } },
        body: { label: body.label, note: body.note || null },
      });
      if (error || !data) throw new Error("Could not save the label.");
      return data;
    },
    // Not the next example: refetching it would replace the one whose decision is on screen.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "eval", "areas"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "eval", "labelled"] });
    },
  });
}
