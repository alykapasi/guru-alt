import { lazy } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { PageShell } from "./PageShell";

vi.mock("./NavBar", () => ({ NavBar: () => <nav aria-label="Main" /> }));
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

  it("keeps its bar while a page's code is still loading", async () => {
    const NeverLoads = lazy(() => new Promise<never>(() => {}));
    render(
      <MemoryRouter initialEntries={["/app/x"]}>
        <Routes>
          <Route path="/app" element={<PageShell />}>
            <Route path="x" element={<NeverLoads />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });
});
