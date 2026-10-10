import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useConversation, useDecideDetour, usePracticeAction } from "./hooks";

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

describe("a practice control the server refuses", () => {
  it("refetches the conversation and transcript so the controls redraw", async () => {
    const { invalidate, wrapper } = setup();
    const { result } = renderHook(() => usePracticeAction("conv-1"), { wrapper });

    result.current.mutate("resume");

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["messages", "conv-1"] });
  });
});

describe("the conversation the chat page has open", () => {
  it("is refetched by everything that refreshes the conversation lists", async () => {
    // Pause, resume, archive and unarchive invalidate ["conversations"]. The open
    // conversation is read by id (S62), so its key has to sit under that prefix or the
    // page keeps showing the phase and archive state from before the action.
    const fetcher = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ id: "c-1", phase: "chatting" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetcher);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useConversation("c-1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetcher).toHaveBeenCalledTimes(1);

    await client.invalidateQueries({ queryKey: ["conversations"] });

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });
});
