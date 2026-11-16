import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { SubjectWizard } from "./SubjectWizard";

/** A new subject starts from what the learner wants to learn; materials are asked for after,
 * as the landing page's "Say what you want to learn" promises. */

beforeEach(() => {
  vi.restoreAllMocks();
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ session_id: "g-1", sources: [], has_more: false }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    ),
  );
});

function renderWizard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <SubjectWizard />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("creating a subject", () => {
  it("asks for the goal first", async () => {
    renderWizard();
    expect(
      await screen.findByRole("heading", { name: "What's your learning goal?" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Add learning materials" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
  });

  it("lists the steps in the order they happen", () => {
    renderWizard();
    const steps = within(screen.getByRole("list", { name: "Steps" })).getAllByRole("listitem");
    expect(steps.map((s) => s.textContent)).toEqual(["Goal", "Materials", "Review", "Create"]);
  });
});
