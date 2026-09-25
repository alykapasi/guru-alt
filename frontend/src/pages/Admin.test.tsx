import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    suspended_at: null,
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
    suspended_at: null,
  },
];

const ROSTER = { total: 2, window_hours: 24, learners: LEARNERS };

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function serve(
  spend: unknown = SPEND,
  roster: unknown = ROSTER,
  log: unknown = [],
  invitations: unknown = [],
) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    // `input` is a Request here, not a string — `toString()` on one yields "[object
    // Request]", which matches every branch and none of them usefully.
    const url =
      typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
    if (url.includes("/admin/impersonations")) return Promise.resolve(jsonResponse(log));
    if (url.includes("/admin/invitations")) return Promise.resolve(jsonResponse(invitations));
    // Unrelated to this file's own assertions, but a non-array answer here crashes
    // ConceptLinkQueue's render rather than failing softly, same as an unstubbed route would.
    if (url.includes("/admin/concept-links")) return Promise.resolve(jsonResponse([]));
    return Promise.resolve(jsonResponse(url.includes("/admin/learners") ? roster : spend));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
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

  it("says when a visit started and ended, whichever way the timestamp is written", async () => {
    // `created_at` arrives without a zone (the timestamp mixin is naive UTC) and `ended_at` with
    // one. Appending "Z" to both made the second an invalid date, and the log read "NaNd ago".
    const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
    serve(SPEND, ROSTER, [
      {
        id: "imp-1",
        admin_learner_id: "a-1",
        admin_handle: "root",
        learner_id: "l-1",
        learner_handle: "active",
        reason: "checking an upload that failed",
        created_at: minutesAgo(10).replace("Z", ""),
        expires_at: minutesAgo(-5),
        ended_at: minutesAgo(3),
      },
    ]);
    renderPortal();

    expect(await screen.findByText("checking an upload that failed")).toBeInTheDocument();
    expect(screen.getByText("10m ago")).toBeInTheDocument();
    expect(screen.getByText("3m ago")).toBeInTheDocument();
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
  });

  it("does not claim truncation when every account is shown", async () => {
    serve();
    renderPortal();

    await screen.findByText("Quiet Learner");
    expect(screen.queryByText(/costliest of/)).not.toBeInTheDocument();
  });
});

/** Invitations and suspension (S21).
 *
 * Guru is invite-only and Clerk's free tier has no ban, so both of these are Guru's own
 * decisions rather than the identity provider's — which makes this panel the entire front
 * door and the only way to close it again.
 */

const INVITATIONS = [
  {
    id: "i-1",
    email: "open@example.com",
    status: "open",
    accepted_at: null,
    revoked_at: null,
    created_at: "2026-09-10T00:00:00",
    invited_by_handle: "ada",
  },
  {
    id: "i-2",
    email: "accepted@example.com",
    status: "accepted",
    accepted_at: "2026-09-11T00:00:00",
    revoked_at: null,
    created_at: "2026-09-10T00:00:00",
    invited_by_handle: "ada",
  },
  {
    id: "i-3",
    email: "revoked@example.com",
    status: "revoked",
    accepted_at: null,
    revoked_at: "2026-09-12T00:00:00",
    created_at: "2026-09-10T00:00:00",
    invited_by_handle: "ada",
  },
];

/** Answers a named write with `status`/`body`, and everything else normally. */
function serveWithRefusal(match: string, status: number, body: unknown) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
    const method = (input instanceof Request ? input.method : init?.method) ?? "GET";
    if (url.includes(match) && method === "POST") {
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
      );
    }
    if (url.includes("/admin/impersonations")) return Promise.resolve(jsonResponse([]));
    if (url.includes("/admin/invitations")) return Promise.resolve(jsonResponse(INVITATIONS));
    if (url.includes("/admin/concept-links")) return Promise.resolve(jsonResponse([]));
    return Promise.resolve(jsonResponse(url.includes("/admin/learners") ? ROSTER : SPEND));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function postsTo(fetchMock: ReturnType<typeof vi.fn>, match: string) {
  return fetchMock.mock.calls.filter(([input, init]) => {
    const url =
      typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
    const method =
      (input instanceof Request ? input.method : (init as RequestInit | undefined)?.method) ??
      "GET";
    return url.includes(match) && method === "POST";
  });
}

describe("who may join", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("lists open, accepted and revoked invitations with their status", async () => {
    // All three, not just the open ones: the question is "did this person get in, and if not
    // why", and a list of only open invitations cannot answer it.
    serve(SPEND, ROSTER, [], INVITATIONS);
    renderPortal();

    expect(await screen.findByText("open@example.com")).toBeInTheDocument();
    expect(await screen.findByText("accepted@example.com")).toBeInTheDocument();
    expect(await screen.findByText("revoked@example.com")).toBeInTheDocument();
    expect(await screen.findByText("accepted")).toBeInTheDocument();
    expect(await screen.findByText("revoked")).toBeInTheDocument();
  });

  it("offers Revoke only on an invitation that is still open", async () => {
    serve(SPEND, ROSTER, [], INVITATIONS);
    renderPortal();
    await screen.findByText("open@example.com");

    // One button for three rows: revoking a spent invitation is not an action that exists.
    expect(screen.getAllByRole("button", { name: "Revoke" })).toHaveLength(1);
  });

  it("posts the address when inviting", async () => {
    const fetchMock = serve(SPEND, ROSTER, [], []);
    renderPortal();

    const field = await screen.findByLabelText("Email to invite");
    fireEvent.change(field, { target: { value: "new@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));

    await waitFor(() => expect(postsTo(fetchMock, "/admin/invitations")).toHaveLength(1));
    const request = postsTo(fetchMock, "/admin/invitations")[0][0] as Request;
    expect(await request.clone().json()).toEqual({ email: "new@example.com" });
  });

  it("shows the server's own reason when an invitation is refused", async () => {
    // "That address is already enrolled" is actionable; "Could not invite" is not.
    serveWithRefusal("/admin/invitations", 409, {
      detail: "that address is already enrolled",
    });
    renderPortal();

    const field = await screen.findByLabelText("Email to invite");
    fireEvent.change(field, { target: { value: "taken@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));

    expect(await screen.findByText("that address is already enrolled")).toBeInTheDocument();
  });

  it("asks before revoking, and does not post when the operator declines", async () => {
    const fetchMock = serve(SPEND, ROSTER, [], INVITATIONS);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPortal();
    await screen.findByText("open@example.com");

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));

    expect(window.confirm).toHaveBeenCalled();
    expect(postsTo(fetchMock, "/revoke")).toHaveLength(0);
  });

  it("revokes when the operator confirms", async () => {
    const fetchMock = serve(SPEND, ROSTER, [], INVITATIONS);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPortal();
    await screen.findByText("open@example.com");

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => expect(postsTo(fetchMock, "/invitations/i-1/revoke")).toHaveLength(1));
  });
});

describe("suspending an account", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("will not suspend without a reason", async () => {
    // Disabled rather than refused after the fact: the API requires a reason, and the audit
    // row is only worth as much as the sentence somebody typed.
    serve();
    renderPortal();
    await screen.findByText("Active Learner");

    fireEvent.click(screen.getAllByRole("button", { name: "Suspend" })[0]);

    const confirm = await screen.findByRole("button", { name: "Suspend account" });
    expect(confirm).toBeDisabled();
  });

  it("posts the reason and the learner it names", async () => {
    const fetchMock = serve();
    renderPortal();
    await screen.findByText("Active Learner");

    fireEvent.click(screen.getAllByRole("button", { name: "Suspend" })[0]);
    fireEvent.change(await screen.findByLabelText("Why?"), {
      target: { value: "Repeated abuse reports" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Suspend account" }));

    await waitFor(() => expect(postsTo(fetchMock, "/l-1/suspend")).toHaveLength(1));
    const request = postsTo(fetchMock, "/l-1/suspend")[0][0] as Request;
    expect(await request.clone().json()).toEqual({ reason: "Repeated abuse reports" });
  });

  it("badges a suspended learner and offers Reinstate instead", async () => {
    const suspended = {
      total: 1,
      window_hours: 24,
      learners: [{ ...LEARNERS[0], suspended_at: "2026-09-15T00:00:00" }],
    };
    const fetchMock = serve(SPEND, suspended, [], []);
    renderPortal();

    expect(await screen.findByText("Suspended")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Suspend" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reinstate" }));

    await waitFor(() => expect(postsTo(fetchMock, "/l-1/reinstate")).toHaveLength(1));
  });

  it("shows the server's own reason when a suspension is refused", async () => {
    serveWithRefusal("/suspend", 409, { detail: "an administrator cannot suspend themselves" });
    renderPortal();
    await screen.findByText("Active Learner");

    fireEvent.click(screen.getAllByRole("button", { name: "Suspend" })[0]);
    fireEvent.change(await screen.findByLabelText("Why?"), { target: { value: "a reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Suspend account" }));

    expect(
      await screen.findByText("an administrator cannot suspend themselves"),
    ).toBeInTheDocument();
  });
});
