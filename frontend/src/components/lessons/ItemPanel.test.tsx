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

const secondFlashcard: ItemEvent = {
  id: "33333333-3333-3333-3333-333333333333",
  item_type: "flashcard",
  stem: "What is the derivative of e^x?",
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
  const wrap = (i: ItemEvent) => (
    <QueryClientProvider client={queryClient}>
      <ItemPanel item={i} detail={null} onRate={onRate} />
    </QueryClientProvider>
  );
  const utils = render(wrap(item));
  // Rerenders the *same mounted tree* with a different item — the scenario a fresh `render`
  // call cannot reproduce, since a new tree would remount FlashcardPanel regardless of `key`.
  return { ...utils, rerenderWithItem: (next: ItemEvent) => utils.rerender(wrap(next)) };
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

  it("does not carry a revealed answer into the next flashcard", async () => {
    // Reproduces the real sequence: useChatConversation's setLiveItem swaps one non-null
    // ItemEvent for another, never unmounting ItemPanel in between (see useChatConversation.ts's
    // `awaiting_reply`/`done` handlers) — so the regression only shows up on a *rerender* of the
    // same tree, not a fresh mount.
    stubReveal("cos x");
    const { rerenderWithItem } = renderPanel(flashcard);
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    expect(await screen.findByText("cos x")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^good$/i })).toBeInTheDocument();

    rerenderWithItem(secondFlashcard);

    expect(screen.getByText(secondFlashcard.stem)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reveal/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^good$/i })).not.toBeInTheDocument();
    expect(screen.queryByText("cos x")).not.toBeInTheDocument();
  });
});
