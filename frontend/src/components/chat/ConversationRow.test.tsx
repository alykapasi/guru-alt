import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ConversationRow } from "./ConversationRow";

vi.mock("../../api/hooks", () => ({
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchive: () => ({ mutate: vi.fn() }),
  useRemovalImpact: () => ({ data: undefined, isLoading: false }),
  useRemove: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));

const conversation = {
  id: "c1",
  title: "Eigenvalues",
  goal: null,
  kind: "chat",
} as unknown as Parameters<typeof ConversationRow>[0]["conversation"];

describe("ConversationRow", () => {
  it("marks the open conversation as the current page", () => {
    render(
      <MemoryRouter>
        <ConversationRow conversation={conversation} active />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Eigenvalues" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("shows its actions to keyboard focus and on touch or narrow screens, not only on hover", () => {
    render(
      <MemoryRouter>
        <ConversationRow conversation={conversation} active={false} />
      </MemoryRouter>,
    );
    const actions = screen.getByRole("button", { name: "Rename conversation" }).parentElement!;
    expect(actions.className).toContain("group-focus-within:opacity-100");
    expect(actions.className).toContain("max-lg:opacity-100");
    expect(actions.className).toContain("pointer-coarse:opacity-100");
  });
});
