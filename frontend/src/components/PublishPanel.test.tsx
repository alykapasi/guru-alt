import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PublishPanel } from "./PublishPanel";

/** The author's panel has one job the API cannot do: say why sharing is unavailable.
 *
 * A "Publish" button that answers 422 with a message the learner has to interpret is the
 * version of this that generates support questions, so the source-derived case is asserted
 * as *not offering the action* rather than as showing an error afterwards.
 *
 * These stub `fetch` rather than the hooks, following Admin.test: the interesting failures
 * live in what the component does with a real response shape, and a stubbed hook would
 * happily agree with a component that never asked. */

const SUBJECT_ID = "11111111-1111-1111-1111-111111111111";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function serve(publications: unknown[] = []) {
  // Declares the parameter even though it goes unused: without it `mock.calls` is typed as an
  // empty tuple, and the assertions below stop compiling — which `npm run build` catches and
  // a passing vitest run does not.
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    void input;
    return Promise.resolve(jsonResponse({ publications }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPanel(sourceDerived = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PublishPanel subjectId={SUBJECT_ID} sourceDerived={sourceDerived} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("the author's publish panel", () => {
  it("offers the action on a subject that can be shared", async () => {
    serve();
    renderPanel();

    expect(await screen.findByRole("button", { name: "Request publication" })).toBeInTheDocument();
  });

  it("explains why a source-derived subject cannot be shared, and offers nothing", async () => {
    serve();
    renderPanel(true);

    expect(await screen.findByText(/stays private/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request publication" })).not.toBeInTheDocument();
  });

  it("shows the reviewer's note on a rejected request", async () => {
    serve([
      {
        id: "p-1",
        status: "rejected",
        author_note: null,
        review_note: "The answer keys are wrong on three items.",
        reviewed_at: "2026-09-20T00:00:00",
        created_at: "2026-09-19T00:00:00",
        published_subject_id: null,
      },
    ]);
    renderPanel();

    expect(await screen.findByText("Not approved.")).toBeInTheDocument();
    expect(await screen.findByText(/answer keys are wrong/)).toBeInTheDocument();
  });

  it("does not offer a second request while one is waiting", async () => {
    serve([
      {
        id: "p-1",
        status: "pending",
        author_note: null,
        review_note: null,
        reviewed_at: null,
        created_at: "2026-09-19T00:00:00",
        published_subject_id: null,
      },
    ]);
    renderPanel();

    expect(await screen.findByText("Waiting for review.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request publication" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Withdraw request" })).toBeInTheDocument();
  });

  it("sends the note the author typed", async () => {
    const fetchMock = serve();
    renderPanel();

    fireEvent.change(await screen.findByLabelText(/Anything the reviewer should know/), {
      target: { value: "Built this over a term." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Request publication" }));

    // openapi-fetch hands `fetch` a built `Request`, not a (url, init) pair — reading
    // `init.method` here finds nothing and the assertion passes vacuously.
    const posted = await waitFor(() => {
      const request = fetchMock.mock.calls
        .map(([input]) => input)
        .find((input): input is Request => input instanceof Request && input.method === "POST");
      expect(request).toBeDefined();
      return request!;
    });

    expect(await posted.clone().text()).toContain("Built this over a term.");
  });
});
