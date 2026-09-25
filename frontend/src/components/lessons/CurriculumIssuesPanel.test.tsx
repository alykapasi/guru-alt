import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CurriculumIssuesPanel } from "./CurriculumIssuesPanel";

const conflict = {
  prereq_kc_id: "kc-b",
  prereq_slug: "b",
  prereq_name: "Vectors",
  kc_id: "kc-a",
  kc_slug: "a",
  kc_name: "Dot product",
};

function stub(conflicts: unknown[], deleteStatusCode: number = 204) {
  const calls: { method: string; url: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input : new Request(String(input), init);
      calls.push({ method: req.method, url: req.url });
      if (req.method === "DELETE") {
        return new Response(JSON.stringify({ detail: "prerequisite not found" }), {
          status: deleteStatusCode,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(conflicts), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CurriculumIssuesPanel subjectId="s-1" />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("curriculum issues", () => {
  it("renders nothing when the graph has no conflicts", async () => {
    stub([]);
    const { container } = renderPanel();
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("names the conflict and removes the ignored prerequisite on request", async () => {
    const calls = stub([conflict]);
    renderPanel();
    expect(await screen.findByText(/Dot product requires Vectors/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove this prerequisite" }));
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === "DELETE" && c.url.endsWith("/kcs/kc-a/prerequisites/kc-b")),
      ).toBe(true),
    );
    // Refetched after the removal.
    await waitFor(() =>
      expect(calls.filter((c) => c.url.endsWith("/prerequisite-conflicts")).length).toBeGreaterThan(
        1,
      ),
    );
  });

  it("shows an error message if removal fails", async () => {
    stub([conflict], 404);
    renderPanel();
    expect(await screen.findByText(/Dot product requires Vectors/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove this prerequisite" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByText(
        /Couldn't remove that prerequisite — it may already have been removed\. Refresh and try again\./,
      ),
    ).toBeInTheDocument();
  });
});
