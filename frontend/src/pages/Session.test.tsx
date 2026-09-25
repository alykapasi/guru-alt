import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { Session } from "./Session";

/** The session page's own decisions about practice (S52): what it lets the learner do while
 * practice is paused, and what it shows once a resume finds the question gone. The hooks it
 * reads are stubbed — this pins the page's wiring, not the transport underneath it. */

const chat = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
const practice = vi.hoisted(() => ({ mutate: vi.fn() }));

vi.mock("../hooks/useChatConversation", () => ({
  useChatConversation: () => chat.state,
}));
vi.mock("../api/hooks", () => ({
  usePracticeAction: () => ({ mutate: practice.mutate, isPending: false }),
}));
vi.mock("../components/chat/MessageList", () => ({ MessageList: () => null }));
vi.mock("../components/lessons/ItemPanel", () => ({
  ItemPanel: ({ ratingDisabled }: { ratingDisabled?: boolean }) => (
    <div data-testid="item-panel" data-rating-disabled={String(!!ratingDisabled)} />
  ),
}));

function conversation(overrides: Record<string, unknown>) {
  chat.state = {
    messages: [{ id: "m-1" }],
    isLoadingMessages: false,
    hasEarlierMessages: false,
    isLoadingEarlier: false,
    loadEarlierMessages: vi.fn(),
    pending: null,
    error: null,
    canRetry: false,
    retry: vi.fn(),
    item: null,
    sessionDetail: null,
    awaitingReply: false,
    practicePaused: false,
    applyPracticeState: vi.fn(),
    send: vi.fn(),
    ...overrides,
  };
}

function renderSession() {
  return render(
    <MemoryRouter initialEntries={["/session/conv-1"]}>
      <Routes>
        <Route path="/session/:conversationId" element={<Session />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  practice.mutate.mockReset();
});

describe("Session while practice is paused", () => {
  it("disables rating: a rating sent now would go to the tutor, not be graded", () => {
    conversation({ practicePaused: true });
    renderSession();
    expect(screen.getByTestId("item-panel")).toHaveAttribute("data-rating-disabled", "true");
  });

  it("allows rating when practice is live", () => {
    conversation({ awaitingReply: true });
    renderSession();
    expect(screen.getByTestId("item-panel")).toHaveAttribute("data-rating-disabled", "false");
  });

  it("shows only the ended notice once a resume finds the question gone", async () => {
    conversation({ practicePaused: true });
    practice.mutate.mockImplementation(
      (_action: string, opts?: { onSuccess?: (state: unknown) => void }) =>
        opts?.onSuccess?.({ phase: "chatting", item: null, prompt: null, ended: true }),
    );
    const view = renderSession();
    await userEvent.click(screen.getByRole("button", { name: "Back to the question" }));
    // The conversation has moved back to chatting by the time the notice shows.
    conversation({});
    view.rerender(
      <MemoryRouter initialEntries={["/session/conv-1"]}>
        <Routes>
          <Route path="/session/:conversationId" element={<Session />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByText("That question no longer fits your plan, so practice ended."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Practice paused")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back to the question" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skip it" })).not.toBeInTheDocument();
  });
});
