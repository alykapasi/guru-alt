import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const restore = vi.fn();
const erase = vi.fn();
vi.mock("../api/hooks", () => ({
  useRestoreAccount: () => ({ mutate: restore, isPending: false, error: null }),
  useEraseAccount: () => ({ mutate: erase, isPending: false, error: null }),
  useExportFiles: () => ({
    data: [{ id: "s1", origin: "notes.txt", file_path: "/api/v1/me/export/sources/s1/file" }],
  }),
}));
const signOut = vi.fn(async () => {});
vi.mock("../auth/session", () => ({ useSignOutEverywhere: () => signOut }));

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

  it("offers the learner's data, and each file, before they go", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    expect(screen.getByRole("link", { name: /Download your data/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "notes.txt" })).toHaveAttribute(
      "href",
      expect.stringContaining("/api/v1/me/export/sources/s1/file"),
    );
  });

  it("can sign out without deciding, so a shared computer is left signed out", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(signOut).toHaveBeenCalled();
  });
});
