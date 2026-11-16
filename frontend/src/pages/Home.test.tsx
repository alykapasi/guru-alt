import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/** Signing in lands on Lessons — where a learner starts a subject and practises — not on an
 * empty chat that says nothing about what to do first. */

vi.mock("../auth/mode", () => ({ clerkEnabled: false, CLERK_PUBLISHABLE_KEY: "" }));

const { SignIn } = await import("./SignIn");
const { SignUp } = await import("./SignUp");

function signedIn() {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({ id: "l-1", handle: "ada", display_name: "Ada", is_admin: false }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    ),
  );
}

function renderAt(path: string, page: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={path} element={page} />
          <Route path="/app/lessons" element={<p>lessons</p>} />
          <Route path="/app/chat" element={<p>chat</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  signedIn();
});

describe("where a signed-in learner lands", () => {
  it("is Lessons, from the sign-in page", async () => {
    renderAt("/signin", <SignIn />);
    expect(await screen.findByText("lessons")).toBeInTheDocument();
  });

  it("is Lessons, from the registration page", async () => {
    renderAt("/sign-up", <SignUp />);
    expect(await screen.findByText("lessons")).toBeInTheDocument();
  });
});
