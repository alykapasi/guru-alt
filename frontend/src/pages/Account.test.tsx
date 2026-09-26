import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const request = vi.fn();
vi.mock("../api/hooks", () => ({
  useRequestDeletion: () => ({ mutate: request, isPending: false, error: null }),
}));
vi.mock("../auth/session", () => ({ useSignOutEverywhere: () => async () => {} }));

import { Account } from "./Account";

describe("Account", () => {
  beforeEach(() => request.mockClear());

  it("explains the window and deletes only after confirming", () => {
    render(<Account />);
    expect(screen.getByText(/erased after 7 days/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    expect(request).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));
    expect(request).toHaveBeenCalledWith({ now: false }, expect.anything());
  });

  it("offers erase now instead, in one request", () => {
    render(<Account />);
    fireEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    fireEvent.click(screen.getByRole("button", { name: "Erase now instead" }));
    expect(request).toHaveBeenCalledWith({ now: true }, expect.anything());
  });
});
