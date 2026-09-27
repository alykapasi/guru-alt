import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE_URL } from "../api/client";

/** The exchange, and the two refusals that must not be confused (S21).
 *
 * Clerk proves who someone is; Guru decides whether they may come in. A 403 is Guru's answer
 * about *this person* and ends their Clerk session too, or they loop through a doomed exchange
 * on every page load. A 502 is Guru unable to reach Clerk to ask — which says nothing about
 * them, so signing them out would turn an outage into a rejection they cannot argue with.
 */

const clerk = vi.hoisted(() => ({
  isLoaded: true,
  isSignedIn: true,
  getToken: vi.fn(async () => "clerk-token-abc"),
  signOut: vi.fn(async () => {}),
  signInProps: null as Record<string, unknown> | null,
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => ({
    isLoaded: clerk.isLoaded,
    isSignedIn: clerk.isSignedIn,
    getToken: clerk.getToken,
  }),
  useClerk: () => ({ signOut: clerk.signOut }),
  SignIn: (props: Record<string, unknown>) => {
    clerk.signInProps = props;
    return <div>clerk sign-in panel</div>;
  },
  SignUp: () => <div>clerk sign-up panel</div>,
  UserButton: () => <div>clerk user button</div>,
}));

vi.mock("./mode", () => ({ clerkEnabled: true, CLERK_PUBLISHABLE_KEY: "pk_test_stub" }));

const { ClerkSessionWatcher, ClerkSignInPanel } = await import("./ClerkPanels");
const { useCurrentLearner } = await import("../api/auth");

/** What the page around the panel sees — the sign-in page leaves as soon as this has a learner. */
function WhoAmI() {
  const { data } = useCurrentLearner();
  return <p>{data ? `signed in as ${data.handle}` : "nobody"}</p>;
}

const LEARNER = {
  id: "l-1",
  handle: "ada",
  display_name: "Ada",
  email: "ada@example.com",
  is_admin: false,
};

/** Answers `/auth/me` with 401 until the exchange succeeds, the way the real server does. */
function server({
  exchange,
  meAfter = LEARNER,
}: {
  exchange: { status: number; body: unknown };
  meAfter?: unknown;
}) {
  let exchanged = false;
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.startsWith(`${API_BASE_URL}/api/v1/auth/exchange`)) {
      if (exchange.status === 200) exchanged = true;
      return new Response(JSON.stringify(exchange.body), {
        status: exchange.status,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.startsWith(`${API_BASE_URL}/api/v1/auth/me`)) {
      return exchanged
        ? new Response(JSON.stringify(meAfter), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : new Response(JSON.stringify({ detail: "not authenticated" }), { status: 401 });
    }
    if (url.startsWith(`${API_BASE_URL}/api/v1/auth/logout`))
      return new Response(null, { status: 204 });
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPanel(ui: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/signin"]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function exchangeCalls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter(([input]) => {
    const url = String(input instanceof Request ? input.url : input);
    return url.includes("/auth/exchange");
  });
}

describe("the Clerk sign-in panel", () => {
  beforeEach(() => {
    clerk.isLoaded = true;
    clerk.isSignedIn = true;
    clerk.getToken.mockClear();
    clerk.signOut.mockClear();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("exchanges the Clerk token once and picks up the Guru learner", async () => {
    const fetchMock = server({ exchange: { status: 200, body: LEARNER } });

    renderPanel(<ClerkSignInPanel />);

    await waitFor(() => expect(exchangeCalls(fetchMock)).toHaveLength(1));
    const request = exchangeCalls(fetchMock)[0][0] as Request;
    expect(request.headers.get("authorization")).toBe("Bearer clerk-token-abc");
    // Once, not once per render: a second exchange races the first's cache clear.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(exchangeCalls(fetchMock)).toHaveLength(1);
  });

  it("tells the page it is signed in once the exchange succeeds, without a reload", async () => {
    server({ exchange: { status: 200, body: LEARNER } });

    renderPanel(
      <>
        <WhoAmI />
        <ClerkSignInPanel />
      </>,
    );

    expect(await screen.findByText("signed in as ada")).toBeInTheDocument();
  });

  it("shows the server's reason and ends the Clerk session when Guru refuses the person", async () => {
    server({
      exchange: { status: 403, body: { detail: "Guru is invite-only. Ask an administrator." } },
    });

    renderPanel(<ClerkSignInPanel />);

    expect(
      await screen.findByText("Guru is invite-only. Ask an administrator."),
    ).toBeInTheDocument();
    await waitFor(() => expect(clerk.signOut).toHaveBeenCalledTimes(1));
  });

  it("keeps the Clerk session when Guru could not reach the provider", async () => {
    // The person is who they said they are; Guru could not ask. Signing them out here would
    // make an outage indistinguishable from a rejection.
    server({
      exchange: { status: 502, body: { detail: "could not reach the identity provider" } },
    });

    renderPanel(<ClerkSignInPanel />);

    expect(await screen.findByText("could not reach the identity provider")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(clerk.signOut).not.toHaveBeenCalled();
  });

  it("does not mount Clerk's panel once Clerk is signed in", async () => {
    // Clerk's panel redirects a signed-in visitor away on mount, which unmounts the exchange
    // before it runs — the page the person was sent to never had a Guru session to show.
    server({ exchange: { status: 200, body: LEARNER } });

    renderPanel(<ClerkSignInPanel />);

    expect(await screen.findByText("Signing you in…")).toBeInTheDocument();
    expect(screen.queryByText("clerk sign-in panel")).not.toBeInTheDocument();
  });

  it("sends Clerk back to this page after sign-in, where the exchange runs", () => {
    clerk.isSignedIn = false;
    server({ exchange: { status: 200, body: LEARNER } });

    renderPanel(<ClerkSignInPanel />);

    expect(screen.getByText("clerk sign-in panel")).toBeInTheDocument();
    expect(clerk.signInProps).toMatchObject({
      forceRedirectUrl: "/signin",
      signUpForceRedirectUrl: "/sign-up",
    });
  });

  it("does not exchange while Clerk is still loading", async () => {
    clerk.isLoaded = false;
    const fetchMock = server({ exchange: { status: 200, body: LEARNER } });

    renderPanel(<ClerkSignInPanel />);

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(exchangeCalls(fetchMock)).toHaveLength(0);
  });
});

describe("the Clerk session watcher", () => {
  beforeEach(() => {
    clerk.isLoaded = true;
    clerk.signOut.mockClear();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("ends Guru's session when Clerk reports signed out", async () => {
    // Signing out in another tab must not leave this one holding a working Guru cookie.
    clerk.isSignedIn = false;
    let exchanged = true;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url.startsWith(`${API_BASE_URL}/api/v1/auth/me`)) {
        return exchanged
          ? new Response(JSON.stringify(LEARNER), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            })
          : new Response(JSON.stringify({ detail: "not authenticated" }), { status: 401 });
      }
      if (url.startsWith(`${API_BASE_URL}/api/v1/auth/logout`)) {
        exchanged = false;
        return new Response(null, { status: 204 });
      }
      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPanel(<ClerkSessionWatcher />);

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input instanceof Request ? input.url : input).includes("/auth/logout"),
        ),
      ).toBe(true),
    );
  });
});
