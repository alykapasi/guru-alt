import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { NavBar } from "./NavBar";

const learner = vi.hoisted(() => ({
  current: { display_name: "Ada", handle: "ada", is_admin: false } as Record<string, unknown>,
}));
vi.mock("../api/auth", () => ({ useCurrentLearner: () => ({ data: learner.current }) }));
vi.mock("../auth/session", () => ({ useSignOutEverywhere: () => async () => {} }));
vi.mock("./ThemeToggle", () => ({ ThemeToggle: () => null }));

function renderBar() {
  return render(
    <MemoryRouter>
      <NavBar />
    </MemoryRouter>,
  );
}

const names = (el: HTMLElement) =>
  within(el)
    .getAllByRole("link")
    .map((a) => a.textContent);

describe("NavBar", () => {
  it("keeps the bar to the places a learner works in, in the order they use them", () => {
    renderBar();
    expect(names(screen.getByRole("navigation", { name: "Main" }))).toEqual([
      "Lessons",
      "Chat",
      "Notes",
      "Uploads",
      "Dashboard",
    ]);
  });

  it("puts the learner's own settings under their name", () => {
    learner.current = { display_name: "Ada", handle: "ada", is_admin: true };
    renderBar();
    const menu = screen.getByRole("group", { name: "Account menu" });
    expect(within(menu).getByText("Ada")).toBeInTheDocument();
    expect(names(menu)).toEqual(["Memory", "Account", "Admin"]);
    expect(within(menu).getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("shows Admin only to an administrator", () => {
    learner.current = { display_name: "Ada", handle: "ada", is_admin: false };
    renderBar();
    expect(names(screen.getByRole("group", { name: "Account menu" }))).toEqual([
      "Memory",
      "Account",
    ]);
  });

  it("folds the main links into a menu for narrow screens", () => {
    renderBar();
    const menu = screen.getByRole("group", { name: "Menu" });
    expect(within(menu).getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/app/chat");
  });

  it("closes either menu once a link is chosen, since the bar stays mounted across navigation", () => {
    renderBar();
    for (const name of ["Menu", "Account menu"]) {
      const menu = screen.getByRole("group", { name });
      menu.setAttribute("open", "");
      fireEvent.click(within(menu).getAllByRole("link")[0]);
      expect(menu).not.toHaveAttribute("open");
    }
  });
});
