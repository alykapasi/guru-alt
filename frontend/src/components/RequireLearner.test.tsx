import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RequireLearner } from "./RequireLearner";
import { API_BASE_URL } from "../api/client";

/** The gate is not the security boundary — the API refuses an unauthenticated request
 * whatever renders here. What these cover is that a signed-out browser is sent to sign in
 * instead of rendering a shell whose every call 401s, and that where they were going
 * survives the detour. */

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<RequireLearner />}>
            <Route path="/app/notes" element={<p>the notes page</p>} />
          </Route>
          <Route path="/signin" element={<p>sign in please</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function answerMe(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url.startsWith(`${API_BASE_URL}/api/v1/auth/me`)) {
        return new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 404 });
    }),
  );
}

describe("the learner gate", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("sends a signed-out browser to sign in", async () => {
    answerMe(401, { detail: "not authenticated" });
    renderAt("/app/notes");
    expect(await screen.findByText("sign in please")).toBeInTheDocument();
  });

  it("lets a signed-in learner through", async () => {
    answerMe(200, { id: "l-1", handle: "ada", display_name: "Ada", email: "ada@example.com" });
    renderAt("/app/notes");
    expect(await screen.findByText("the notes page")).toBeInTheDocument();
  });

  it("sends the session cookie with the check", async () => {
    // Without `credentials: "include"` the browser holds a valid session and every request is
    // still a 401 — which reads as a broken login rather than a missing option.
    answerMe(200, { id: "l-1", handle: "ada", display_name: null, email: null });
    renderAt("/app/notes");
    await screen.findByText("the notes page");
    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const init = call[1] as RequestInit | undefined;
    const credentials =
      init?.credentials ?? (call[0] instanceof Request ? call[0].credentials : undefined);
    expect(credentials).toBe("include");
  });
});
