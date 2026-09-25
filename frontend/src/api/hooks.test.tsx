import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useDecideDetour } from "./hooks";

/** A mutation that fails is not followed by a refetch in React Query. Where a 409 means "the
 * thing you clicked on is gone", the hook has to invalidate what it drew from itself, or the
 * learner is left staring at a control that can only fail again. */

function conflict() {
  return new Response(JSON.stringify({ detail: "conflict" }), {
    status: 409,
    headers: { "content-type": "application/json" },
  });
}

function setup() {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(conflict())),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { invalidate, wrapper };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("a detour decision the server refuses", () => {
  it("refetches the plan so the stale offer goes away", async () => {
    const { invalidate, wrapper } = setup();
    const { result } = renderHook(() => useDecideDetour("subj-1"), { wrapper });

    result.current.mutate({ prereqKcId: "kc-1", decision: "accept" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["lesson-plan", "subj-1"] });
  });
});
