import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Memory } from "./Memory";

/** The point of this page is that a learner can disagree with the system about themselves.
 * These hold the part that makes that true: the correction reaches the server as a correction,
 * and the destructive action is not one click away from the ordinary one. */

const MEMORY = {
  id: "m-1",
  kind: "fact",
  content: "Prefers worked examples before definitions",
  conversation_id: null,
  created_at: "2026-09-01T00:00:00Z",
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Memory />
    </QueryClientProvider>,
  );
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("seeing what is remembered", () => {
  it("shows each entry so a wrong one can be recognised", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse([MEMORY]))),
    );
    renderPage();
    expect(
      await screen.findByText("Prefers worked examples before definitions"),
    ).toBeInTheDocument();
  });

  it("says so plainly when there is nothing, rather than showing an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    );
    renderPage();
    expect(await screen.findByText(/Nothing yet/)).toBeInTheDocument();
  });
});

describe("correcting what is wrong", () => {
  it("sends the new text as a correction, not as a deletion", async () => {
    const fetchMock = vi.fn((input: Request | string) => {
      const request = input as Request;
      if (request.method === "PATCH") {
        return Promise.resolve(jsonResponse({ ...MEMORY, id: "m-2", content: "Corrected" }));
      }
      return Promise.resolve(jsonResponse([MEMORY]));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    await userEvent.click(await screen.findByLabelText(/^Correct:/));
    const box = screen.getByLabelText("What is actually true");
    await userEvent.clear(box);
    await userEvent.type(box, "Corrected");
    await userEvent.click(screen.getByLabelText("Save correction"));

    await waitFor(() => {
      const patch = fetchMock.mock.calls
        .map(([input]) => input as Request)
        .find((r) => r.method === "PATCH");
      expect(patch).toBeDefined();
    });
    const deletes = fetchMock.mock.calls
      .map(([input]) => input as Request)
      .filter((r) => r.method === "DELETE");
    expect(deletes).toHaveLength(0);
  });

  it("sends nothing when the text was not actually changed", async () => {
    // Typed argument, unused on purpose: a no-arg mock types mock.calls as an empty tuple, and
    // these tests exist to inspect the requests that were (or were not) made.
    const fetchMock = vi.fn((input: Request | string) =>
      Promise.resolve(jsonResponse([MEMORY, input].slice(0, 1))),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    await userEvent.click(await screen.findByLabelText(/^Correct:/));
    await userEvent.click(screen.getByLabelText("Save correction"));

    const patches = fetchMock.mock.calls
      .map(([input]) => input as Request)
      .filter((r) => r.method === "PATCH");
    expect(patches).toHaveLength(0);
  });
});

describe("forgetting everything", () => {
  it("asks first, because it is not reversible from here", async () => {
    // Typed argument, unused on purpose: a no-arg mock types mock.calls as an empty tuple, and
    // these tests exist to inspect the requests that were (or were not) made.
    const fetchMock = vi.fn((input: Request | string) =>
      Promise.resolve(jsonResponse([MEMORY, input].slice(0, 1))),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Forget everything" }));
    const deletes = fetchMock.mock.calls
      .map(([input]) => input as Request)
      .filter((r) => r.method === "DELETE");
    expect(deletes).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Yes, forget everything" })).toBeInTheDocument();
  });
});
