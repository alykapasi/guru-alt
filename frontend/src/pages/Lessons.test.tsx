import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Lessons } from "./Lessons";

/** Adding a subject must not depend on having none: the shared library is visible to everyone,
 * so a list that is never empty used to leave no way in at all. */

const SUBJECT = {
  id: "s-1",
  slug: "financial-machine-learning",
  name: "Financial Machine Learning",
  description: null,
  owner_learner_id: null,
  private_source_derived: false,
};

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function renderPage(subjects: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      return Promise.resolve(json(url.match(/\/subjects(\?|$)/) ? subjects : []));
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Lessons />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("adding a subject", () => {
  it("is offered when subjects already exist", async () => {
    renderPage([SUBJECT]);
    const link = await screen.findByRole("link", { name: /New subject/ });
    expect(link).toHaveAttribute("href", "/app/subjects/new");
  });

  it("is offered when there are none", async () => {
    renderPage([]);
    const link = await screen.findByRole("link", { name: /first subject/ });
    expect(link).toHaveAttribute("href", "/app/subjects/new");
  });
});

describe("a subject of one's own", () => {
  const OWNED = { ...SUBJECT, id: "s-2", owner_learner_id: "me" };

  it("keeps sources and sharing folded away until asked for", async () => {
    renderPage([OWNED]);
    const section = await screen.findByText("Sources and sharing");
    expect(screen.getByRole("button", { name: "Request publication" })).not.toBeVisible();
    fireEvent.click(section);
    expect(screen.getByRole("button", { name: "Request publication" })).toBeVisible();
  });
});
