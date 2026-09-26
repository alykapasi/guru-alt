import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const restore = vi.fn();
const erase = vi.fn();
vi.mock("../api/hooks", () => ({
  useRestoreAccount: () => ({ mutate: restore, isPending: false, error: null }),
  useEraseAccount: () => ({ mutate: erase, isPending: false, error: null }),
}));
vi.mock("../auth/session", () => ({ useSignOutEverywhere: () => async () => {} }));

import { Recovery } from "./Recovery";

describe("Recovery", () => {
  it("says when the account goes, and restores it", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    expect(screen.getByText(/scheduled for deletion/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore my account" }));
    expect(restore).toHaveBeenCalled();
  });

  it("erases only after a second, explicit confirmation", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    fireEvent.click(screen.getByRole("button", { name: "Erase now" }));
    expect(erase).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Yes, erase everything now" }));
    expect(erase).toHaveBeenCalled();
  });

  it("offers the learner's data before they go", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    expect(screen.getByRole("link", { name: /Download your data/ })).toBeInTheDocument();
  });
});
