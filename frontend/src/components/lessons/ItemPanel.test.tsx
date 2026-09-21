import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ItemPanel } from "./ItemPanel";
import type { ItemEvent } from "../../api/sse";

/** The branch this pins: guided practice hands a flashcard to FlashcardPanel's think-reveal-rate
 * flow instead of the bare stem every other item type gets. A guard nobody watched fail is not
 * a guard anyone has tested (S54). */

const flashcard: ItemEvent = {
  id: "11111111-1111-1111-1111-111111111111",
  item_type: "flashcard",
  stem: "What is the derivative of sin x?",
  difficulty: 0.5,
  rubric_id: null,
  kcs: [],
};

const mcq: ItemEvent = {
  id: "22222222-2222-2222-2222-222222222222",
  item_type: "mcq",
  stem: "Which of these is a prime number?",
  difficulty: 0.5,
  rubric_id: null,
  kcs: [],
};

function stubReveal(back: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ back }), { status: 200 })),
  );
}

function renderPanel(item: ItemEvent, onRate = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ItemPanel item={item} detail={null} onRate={onRate} />
    </QueryClientProvider>,
  );
}

describe("ItemPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("delegates a flashcard item to FlashcardPanel's reveal-then-rate flow", async () => {
    stubReveal("cos x");
    renderPanel(flashcard);
    expect(screen.getByRole("button", { name: /reveal/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    expect(await screen.findByRole("button", { name: /^good$/i })).toBeInTheDocument();
  });

  it("renders a non-flashcard item's stem directly, with no reveal step", () => {
    renderPanel(mcq);
    expect(screen.getByText(mcq.stem)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reveal/i })).not.toBeInTheDocument();
  });
});
