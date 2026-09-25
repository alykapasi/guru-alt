import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConceptLinkQueue } from "./ConceptLinkQueue";

/** The administrator is ruling on pairs of curated components that share a concept name. An
 * endorsement is half a link — the reason is what a learner reads when deciding whether to use
 * it — so the tests that matter are that the reason is required before a verdict can be sent,
 * and that a link already decided shows its verdict rather than controls a second decision
 * would conflict with. */

const row = {
  id: "l-1",
  kc_a_id: "a",
  kc_b_id: "b",
  kc_a_name: "Matrices",
  kc_b_name: "Matrices",
  subject_a_name: "Linear Algebra",
  subject_b_name: "Graphics",
  verdict: null,
  reason: null,
  decided_at: null,
};

function stub(rows: unknown[], decisionStatusCode: number = 200) {
  const calls: { method: string; url: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input : new Request(String(input), init);
      calls.push({ method: req.method, url: req.url });
      if (req.method === "POST") {
        if (decisionStatusCode >= 400) {
          return new Response(JSON.stringify({ detail: "link already decided" }), {
            status: decisionStatusCode,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ ...row, verdict: "endorsed" }), {
          status: decisionStatusCode,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(rows), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

/** For the two load-failure tests: the GET response is the only thing under test, so a plain
 * stub of it (no method branching) is enough. */
function serveGet(body: unknown, status: number = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
}

function renderQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConceptLinkQueue />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the concept-link queue", () => {
  it("needs a reason before a verdict can be sent", async () => {
    const calls = stub([row]);
    renderQueue();
    expect(await screen.findByText(/Matrices \(Linear Algebra\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Endorse" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason"), "Same object in both.");
    await userEvent.click(screen.getByRole("button", { name: "Endorse" }));
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === "POST" && c.url.endsWith("/admin/concept-links/l-1")),
      ).toBe(true),
    );
  });

  it("shows a decided link's verdict instead of controls", async () => {
    stub([
      { ...row, verdict: "rejected", reason: "Different use.", decided_at: "2026-09-25T10:00:00Z" },
    ]);
    renderQueue();
    expect(await screen.findByText("Rejected — Different use.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Endorse" })).toBeNull();
  });

  it("shows an alert if a verdict fails to save", async () => {
    stub([row], 409);
    renderQueue();
    await userEvent.type(await screen.findByLabelText("Reason"), "Same object in both.");
    await userEvent.click(screen.getByRole("button", { name: "Endorse" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByText("Couldn't save that verdict. Refresh and try again."),
    ).toBeInTheDocument();
  });

  it("shows an alert rather than throwing when the queue fails to load", async () => {
    serveGet({ detail: "internal error" }, 500);
    renderQueue();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByText("Couldn't load concept links. Refresh to try again."),
    ).toBeInTheDocument();
  });

  it("shows the same alert rather than throwing when the response isn't a list", async () => {
    serveGet({});
    renderQueue();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByText("Couldn't load concept links. Refresh to try again."),
    ).toBeInTheDocument();
  });
});
