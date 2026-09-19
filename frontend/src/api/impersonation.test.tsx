import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ImpersonationBanner } from "../components/ImpersonationBanner";
import { api, apiFetch } from "./client";
import { endVisit, startVisit, visitToken } from "./impersonation";

/** The plumbing that decides whose account an administrator is actually looking at (P10).
 *
 * The dangerous failure here is not a refusal, it is a silent success: a banner saying
 * "viewing alice" over the administrator's own dashboard, because the token never reached the
 * request. So the assertions are on the header that goes out, not on what renders.
 */

const VISIT = {
  impersonationId: "imp-1",
  learnerId: "l-1",
  learnerHandle: "alice",
  token: "visit-token-123",
  expiresAt: "2026-09-15T12:00:00",
};

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function captureFetch() {
  const calls: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(input instanceof Request ? input : new Request(String(input), init));
      return Promise.resolve(jsonResponse({}));
    }),
  );
  return calls;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  endVisit();
});

describe("carrying the visit credential", () => {
  it("sends no authorization header when nobody is viewing an account", async () => {
    const calls = captureFetch();

    await api.GET("/api/v1/auth/me");

    expect(visitToken()).toBeNull();
    expect(calls[0].headers.get("authorization")).toBeNull();
  });

  it("sends the visit token as a bearer once a visit has started", async () => {
    startVisit(VISIT);
    const calls = captureFetch();

    await api.GET("/api/v1/auth/me");

    expect(calls[0].headers.get("authorization")).toBe(`Bearer ${VISIT.token}`);
  });

  it("carries it on the hand-rolled calls too", async () => {
    // The SSE turn and the raw-markdown endpoints go through `apiFetch`, not the typed
    // client. A visit that reached one and not the other would show a learner's chat list
    // beside somebody else's transcript.
    startVisit(VISIT);
    const calls = captureFetch();

    await apiFetch("/api/v1/conversations");

    expect(calls[0].headers.get("authorization")).toBe(`Bearer ${VISIT.token}`);
  });

  it("stops sending it when the visit ends", async () => {
    startVisit(VISIT);
    endVisit();
    const calls = captureFetch();

    await api.GET("/api/v1/auth/me");

    expect(calls[0].headers.get("authorization")).toBeNull();
  });
});

function renderBanner() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ImpersonationBanner />
    </QueryClientProvider>,
  );
}

describe("the banner", () => {
  it("renders nothing when nobody is viewing an account", () => {
    const { container } = renderBanner();
    expect(container).toBeEmptyDOMElement();
  });

  it("names the account and says it is read only", () => {
    startVisit(VISIT);
    renderBanner();

    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText(/actions are recorded/i)).toBeInTheDocument();
  });

  it("ends the visit as the administrator, not as the account being viewed", async () => {
    // `/auth/logout` would clear the session cookie in this browser, which is the
    // administrator's own — so stopping a visit would sign them out of their own account.
    startVisit(VISIT);
    const calls = captureFetch();
    renderBanner();

    screen.getByRole("button", { name: "Stop viewing" }).click();

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toContain(`/admin/impersonations/${VISIT.impersonationId}`);
    // And without the visit's own bearer: the route takes an administrator and refuses a visit,
    // so a DELETE that still carried the token would 403 — leaving the record open and the
    // session live — while this browser dropped the token and looked as if it had worked.
    expect(calls[0].headers.get("authorization")).toBeNull();
    expect(calls.some((c) => c.url.includes("/auth/logout"))).toBe(false);
    await waitFor(() => expect(visitToken()).toBeNull());
  });
});
