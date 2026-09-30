import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const request = vi.fn();
const setMemory = vi.hoisted(() => vi.fn());
vi.mock("../api/hooks", () => ({
  useMemorySetting: () => ({ data: { remember: true } }),
  useSetMemorySetting: () => ({ mutate: setMemory, isPending: false }),
  useRequestDeletion: () => ({ mutate: request, isPending: false, error: null }),
  useExportFiles: () => ({ data: [] }),
  usePreferences: () => ({ data: [] }),
  useSetPreference: () => ({ mutate: vi.fn(), isPending: false, error: null }),
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

  it("lets the learner pause memory", () => {
    render(<Account />);
    fireEvent.click(
      screen.getByRole("checkbox", { name: /Remember things from my conversations/ }),
    );
    expect(setMemory).toHaveBeenCalledWith(false);
  });

  it("says exactly what pausing memory does", () => {
    render(<Account />);
    expect(screen.getByText(/won't save new memories from your conversations/)).toBeInTheDocument();
    expect(
      screen.getByText(/stops reading what you type.*answers still count/),
    ).toBeInTheDocument();
  });
});
