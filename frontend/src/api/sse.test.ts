import { afterEach, describe, expect, it, vi } from "vitest";
import { streamTurn } from "./sse";

describe("a refused turn", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("throws the server's message, not a status line", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              detail: {
                code: "budget_exceeded",
                scope: "learner",
                message: "You've reached today's usage limit. It resets over the next 24 hours.",
              },
            }),
            { status: 429, headers: { "content-type": "application/json" } },
          ),
        ),
      ),
    );
    const turn = streamTurn("c-1", { content: "hi" } as never);
    await expect(turn.next()).rejects.toThrow("You've reached today's usage limit");
  });
});
