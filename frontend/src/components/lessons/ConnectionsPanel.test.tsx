import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConnectionsPanel } from "./ConnectionsPanel";

const suggestion = {
  link_id: "l-1",
  reason: "Both are the rate of change.",
  endorsed_by: "judge",
  decision: null,
  a: { kc_id: "k-a", kc_name: "Derivatives", subject_id: "s-calc", subject_name: "Calculus" },
  b: { kc_id: "k-b", kc_name: "Derivatives", subject_id: "s-phys", subject_name: "Physics" },
};

function stub(suggestions: unknown[], decisionStatusCode: number = 200) {
  const calls: { method: string; url: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input : new Request(String(input), init);
      calls.push({ method: req.method, url: req.url });
      if (req.method === "POST") {
        if (decisionStatusCode >= 400) {
          return new Response(JSON.stringify({ detail: "link not endorsed" }), {
            status: decisionStatusCode,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ ...suggestion, decision: "accepted" }), {
          status: decisionStatusCode,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(suggestions), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

function renderPanel(subjectId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConnectionsPanel subjectId={subjectId} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("connections panel", () => {
  it("offers a suggestion touching this subject, naming the other side and the reason", async () => {
    stub([suggestion]);
    renderPanel("s-phys");
    expect(await screen.findByText(/Derivatives in Calculus/)).toBeInTheDocument();
    expect(screen.getByText("Both are the rate of change.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use it here" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Not the same" })).toBeInTheDocument();
  });

  it("ignores suggestions about other subjects", async () => {
    stub([suggestion]);
    const { container } = renderPanel("s-other");
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("accepts, and offers revoke on an accepted link", async () => {
    const calls = stub([{ ...suggestion, decision: "accepted" }]);
    renderPanel("s-phys");
    await userEvent.click(await screen.findByRole("button", { name: "Stop using it" }));
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === "POST" && c.url.endsWith("/concept-links/l-1/decision")),
      ).toBe(true),
    );
  });

  it("shows an alert if a decision fails", async () => {
    stub([suggestion], 409);
    renderPanel("s-phys");
    await userEvent.click(await screen.findByRole("button", { name: "Use it here" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByText("Couldn't save that choice. Refresh and try again."),
    ).toBeInTheDocument();
  });
});
