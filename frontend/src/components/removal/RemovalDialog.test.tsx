import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const mutate = vi.fn();
vi.mock("../../api/hooks", () => ({
  useRemovalImpact: () => ({
    data: {
      kept: { lessons: 1, cited_replies: 0 },
      forgettable: { lessons: 1 },
      notes: ["Your answers and progress stay — they are evidence of what you can do."],
    },
    isLoading: false,
  }),
  useRemove: () => ({ mutate, isPending: false, error: null }),
}));

import { RemovalDialog } from "./RemovalDialog";

describe("RemovalDialog", () => {
  it("says what stays and deletes with or without forgetting", () => {
    render(<RemovalDialog kind="source" id="s1" name="notes.pdf" open onClose={() => {}} />);
    expect(screen.getAllByText("1 lesson built from this").length).toBeGreaterThan(0);
    expect(screen.getByText(/evidence of what you can do/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mutate).toHaveBeenLastCalledWith({ id: "s1", forget: false }, expect.anything());

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mutate).toHaveBeenLastCalledWith({ id: "s1", forget: true }, expect.anything());
  });

  it("does not call something kept once forgetting it is ticked", () => {
    render(<RemovalDialog kind="source" id="s1" name="notes.pdf" open onClose={() => {}} />);
    expect(screen.getByText("1 lesson built from this")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox"));

    expect(screen.queryByText("1 lesson built from this")).not.toBeInTheDocument();
    expect(screen.getByText("Removes 1 lesson built from this.")).toBeInTheDocument();
  });

  it("opens as a modal and closes on Escape", () => {
    const onClose = vi.fn();
    render(<RemovalDialog kind="source" id="s1" name="notes.pdf" open onClose={onClose} />);
    const dialog = screen.getByRole("dialog", { name: "Delete notes.pdf?" });
    expect(dialog).toHaveAttribute("data-modal", "true");
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
