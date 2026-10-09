import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PageShell } from "./PageShell";

vi.mock("./NavBar", () => ({ NavBar: () => null }));
vi.mock("./ImpersonationBanner", () => ({ ImpersonationBanner: () => null }));

describe("PageShell", () => {
  it("lets a keyboard user skip straight to the page's content", () => {
    render(
      <MemoryRouter>
        <PageShell />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
    // Focusable from script, so the skip link and a closed citation can land focus there.
    expect(screen.getByRole("main")).toHaveAttribute("tabindex", "-1");
  });
});
