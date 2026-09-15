import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Admin } from "./Admin";

/** The portal's job is to report numbers that are true, and both of the numbers it shows have
 * a way of being false that looks fine (P10).
 *
 * These stub `fetch` rather than the hooks, for the reason MaterialsStep's do: the interesting
 * failures live in what the page does with a real response shape, and a stubbed hook would
 * happily agree with a page that never asked. */

const EMPTY_LATENCY = { calls: 0, p50_ms: null, p95_ms: null };

const SPEND = {
  window_hours: 24,
  since: "2026-09-14T00:00:00",
  calls: 4,
  input_tokens: 100,
  output_tokens: 50,
  cost_usd: 1.5,
  unpriced_calls: 0,
  budget_usd: null,
  over_budget: false,
  completion: { calls: 2, p50_ms: 200, p95_ms: 1400 },
  first_token: { calls: 2, p50_ms: 90, p95_ms: 320 },
  by_role: [
    {
      name: "smart",
      calls: 2,
      input_tokens: 80,
      output_tokens: 40,
      cost_usd: 1.5,
      unpriced_calls: 0,
      completion: EMPTY_LATENCY,
      first_token: { calls: 2, p50_ms: 90, p95_ms: 320 },
    },
  ],
  by_model: [],
};

const LEARNERS = [
  {
    id: "l-1",
    handle: "active",
    display_name: "Active Learner",
    email: "active@example.com",
    is_admin: false,
    created_at: "2026-09-01T00:00:00",
    calls: 4,
    cost_usd: 1.5,
    unpriced_calls: 0,
    last_call_at: null,
  },
  {
    id: "l-2",
    handle: "quiet",
    display_name: "Quiet Learner",
    email: "quiet@example.com",
    is_admin: false,
    created_at: "2026-09-02T00:00:00",
    calls: 0,
    cost_usd: 0,
    unpriced_calls: 0,
    last_call_at: null,
  },
];

const ROSTER = { total: 2, window_hours: 24, learners: LEARNERS };

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function serve(spend: unknown = SPEND, roster: unknown = ROSTER) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      // `input` is a Request here, not a string — `toString()` on one yields "[object
      // Request]", which matches every branch and none of them usefully.
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
      return Promise.resolve(jsonResponse(url.includes("/admin/learners") ? roster : spend));
    }),
  );
}

function renderPortal() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Admin />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("the operator's portal", () => {
  it("shows the spend and both timings", async () => {
    serve();
    renderPortal();

    // Three times over: the headline, the role it went on, and the learner who incurred it.
    expect(await screen.findAllByText("$1.50")).toHaveLength(3);
    expect(await screen.findByText(/p50 200ms/)).toBeInTheDocument();
    expect(await screen.findByText(/p50 90ms/)).toBeInTheDocument();
  });

  it("says a timing was not measured rather than showing it as zero", async () => {
    serve({ ...SPEND, completion: EMPTY_LATENCY });
    renderPortal();

    expect(await screen.findByText("Not measured")).toBeInTheDocument();
    expect(screen.queryByText(/p50 0ms/)).not.toBeInTheDocument();
  });

  it("names the population a percentile was computed over", async () => {
    serve();
    renderPortal();

    expect(await screen.findAllByText(/over 2 calls/)).not.toHaveLength(0);
  });

  it("warns that spend is a floor when an unpriced model ran", async () => {
    serve({ ...SPEND, unpriced_calls: 3 });
    renderPortal();

    expect(await screen.findByText(/floor rather than the figure/)).toBeInTheDocument();
  });

  it("does not warn about a floor when everything had a price", async () => {
    serve();
    renderPortal();

    await screen.findAllByText("$1.50");
    expect(screen.queryByText(/floor rather than the figure/)).not.toBeInTheDocument();
  });

  it("lists a learner who made no calls at all", async () => {
    serve();
    renderPortal();

    expect(await screen.findByText("Quiet Learner")).toBeInTheDocument();
  });

  it("says so when the list is only part of the accounts", async () => {
    // The ordering puts the learners with no calls last, so the cap removes exactly the rows
    // the list exists to surface — and a truncated page otherwise looks like a complete one.
    serve(SPEND, { ...ROSTER, total: 188 });
    renderPortal();

    expect(await screen.findByText(/costliest of 188 accounts/)).toBeInTheDocument();
  });

  it("does not claim truncation when every account is shown", async () => {
    serve();
    renderPortal();

    await screen.findByText("Quiet Learner");
    expect(screen.queryByText(/costliest of/)).not.toBeInTheDocument();
  });
});
