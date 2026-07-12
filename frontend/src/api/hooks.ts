import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

/** Proof-of-life for the generated typed client + TanStack Query talking to the real backend
 * (through the stub-auth seam) — not just compiling against the schema. Real usage of
 * conversations lands with the chat screen in a later slice. */
export function useConversations() {
  return useQuery({
    queryKey: ["conversations"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/conversations");
      if (error) throw error;
      return data;
    },
  });
}
