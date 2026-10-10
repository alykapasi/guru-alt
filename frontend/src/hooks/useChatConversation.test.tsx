import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { TurnEvent } from "../api/sse";
import { useChatConversation } from "./useChatConversation";

/** Stop (S47) addresses a turn by the id the stream's first frame carries. The learner can
 * press it before that frame arrives — the server may still be choosing a flow — and the
 * press must not be lost. */

const sse = vi.hoisted(() => ({
  stopTurn: vi.fn(() => Promise.resolve()),
  frames: [] as Array<(ev: unknown) => void>,
}));

vi.mock("../api/sse", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/sse")>()),
  stopTurn: sse.stopTurn,
  // Each frame waits until the test hands it over, so the test decides when the turn id lands.
  streamTurn: async function* () {
    for (;;) {
      const ev = await new Promise<TurnEvent>((resolve) =>
        sse.frames.push(resolve as (ev: unknown) => void),
      );
      yield ev;
      if (ev.type === "stopped" || ev.type === "error") return;
    }
  },
}));
vi.mock("../api/hooks", () => ({
  useConversation: () => ({ data: undefined, isLoading: false }),
  useItem: () => ({ data: undefined }),
  useMessages: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

async function nextFrame(ev: TurnEvent) {
  await waitFor(() => expect(sse.frames.length).toBeGreaterThan(0));
  await act(async () => sse.frames.shift()!(ev));
}

describe("useChatConversation stop", () => {
  it("sends a Stop pressed before the turn is named once it is", async () => {
    const { result } = renderHook(() => useChatConversation("c-1"), { wrapper });

    let sending: Promise<void> = Promise.resolve();
    act(() => {
      sending = result.current.send("Hi", { mode: "chat" });
    });
    await waitFor(() => expect(result.current.pending).not.toBeNull());
    act(() => result.current.stop());
    expect(sse.stopTurn).not.toHaveBeenCalled();

    await nextFrame({ type: "turn", turn_id: "t-1" });
    expect(sse.stopTurn).toHaveBeenCalledWith("c-1", "t-1");

    await nextFrame({ type: "stopped", message_id: null });
    await act(() => sending);
  });
  it("remembers how long a busy provider asked to wait", async () => {
    const { result } = renderHook(() => useChatConversation("c-1"), { wrapper });

    let sending: Promise<void> = Promise.resolve();
    act(() => {
      sending = result.current.send("Hi", { mode: "chat" });
    });
    await nextFrame({ type: "turn", turn_id: "t-2" });
    await nextFrame({
      type: "error",
      detail: "The tutor is busy",
      code: "provider_busy",
      retry_after: 7,
    });
    await act(() => sending);

    expect(result.current.error).toBe("The tutor is busy");
    expect(result.current.retryAfter).toBe(7);
  });
});
