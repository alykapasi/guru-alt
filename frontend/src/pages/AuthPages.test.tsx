import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/** The auth pages are pages (S21).
 *
 * `/sign-up` used to route straight at `ClerkSignUpPanel`, so registration rendered a
 * third-party card on an empty document — no logo, no way back to the site, no theme control.
 * Nothing caught it: the panel's own tests passed, because the panel was never the broken part.
 * These assert the frame around it, which is the thing that was missing.
 *
 * Clerk is stubbed the way `ClerkPanels.test.tsx` stubs it. What is under test is what Guru
 * renders, not what Clerk renders inside it.
 */

vi.mock("@clerk/react", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: false, getToken: vi.fn() }),
  useClerk: () => ({ signOut: vi.fn() }),
  SignIn: () => <div data-testid="clerk-panel">clerk sign-in panel</div>,
  SignUp: () => <div data-testid="clerk-panel">clerk sign-up panel</div>,
  UserButton: () => <div>clerk user button</div>,
}));

vi.mock("../auth/mode", () => ({ clerkEnabled: true, CLERK_PUBLISHABLE_KEY: "pk_test_stub" }));

const { SignIn } = await import("./SignIn");
const { SignUp } = await import("./SignUp");

function renderPage(page: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{page}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  // Signed out, the way the server answers before an exchange.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ detail: "unauthenticated" }), { status: 401 })),
  );
});

describe("the registration page", () => {
  it("wraps the panel in the product, rather than being a bare panel", async () => {
    renderPage(<SignUp />);

    expect(await screen.findByTestId("clerk-panel")).toBeInTheDocument();
    // The three things a naked panel had none of.
    expect(screen.getByRole("link", { name: "Guru home" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("heading", { name: "Create your account" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /theme/i })).toBeInTheDocument();
  });

  it("says the invitation is still required, before the work of signing up", async () => {
    renderPage(<SignUp />);

    expect(await screen.findByText(/invite-only/)).toBeInTheDocument();
    expect(screen.getByText(/will not let you in/)).toBeInTheDocument();
  });
});

describe("the sign-in page", () => {
  it("wraps the panel in the product too", async () => {
    renderPage(<SignIn />);

    expect(await screen.findByTestId("clerk-panel")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Guru home" })).toHaveAttribute("href", "/");
  });

  it("states the page's purpose exactly once", async () => {
    renderPage(<SignIn />);
    await screen.findByTestId("clerk-panel");

    // Clerk renders its own title from the provider's application name, which sat directly
    // under ours and read as two headings for one form. `clerk.css` hides Clerk's; this fails
    // if the page ever grows a second one of its own.
    expect(screen.getAllByRole("heading")).toHaveLength(1);
  });
});
