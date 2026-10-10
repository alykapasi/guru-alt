import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { NavBar } from "./NavBar";

vi.mock("../api/auth", () => ({
  useCurrentLearner: () => ({ data: { display_name: "Ada", handle: "ada", is_admin: false } }),
}));
vi.mock("../auth/session", () => ({ useSignOutEverywhere: () => async () => {} }));
vi.mock("./ThemeToggle", () => ({ ThemeToggle: () => null }));

describe("NavBar", () => {
  it("folds the links into a menu for narrow screens", () => {
    render(
      <MemoryRouter>
        <NavBar />
      </MemoryRouter>,
    );
    const menu = screen.getByRole("group", { name: "Menu" });
    expect(within(menu).getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/app/chat");
    expect(within(menu).getByRole("link", { name: "Account" })).toBeInTheDocument();
  });

  it("closes the menu once a link is chosen, since the bar stays mounted across navigation", () => {
    render(
      <MemoryRouter>
        <NavBar />
      </MemoryRouter>,
    );
    const menu = screen.getByRole("group", { name: "Menu" });
    menu.setAttribute("open", "");
    fireEvent.click(within(menu).getByRole("link", { name: "Notes" }));
    expect(menu).not.toHaveAttribute("open");
  });
});
