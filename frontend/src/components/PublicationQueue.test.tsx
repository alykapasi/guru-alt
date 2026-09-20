import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PublicationQueue } from "./PublicationQueue";

/** The reviewer is deciding what enters the shared library, so the queue has to show what
 * would actually ship — including answer keys, which is the part a summary would drop.
 *
 * The exclusion test is the one with teeth: unticking an item has to reach the request body,
 * because a checkbox that looks right and sends nothing approves the item anyway. */

const PUBLICATION = {
  id: "22222222-2222-2222-2222-222222222222",
  status: "pending",
  author_handle: "author",
  author_note: "Built this over a term.",
  review_note: null,
  reviewed_at: null,
  created_at: "2026-09-19T00:00:00",
  published_subject_id: null,
  reviewer_handle: null,
  snapshot: {
    subject: { name: "Calculus", description: null },
    topics: [{ id: "t-1", slug: "integrals", name: "Integrals", description: null }],
    kcs: [
      { id: "k-1", topic_id: "t-1", slug: "subs", name: "Substitution", description: null },
      { id: "k-2", topic_id: "t-1", slug: "anti", name: "Antiderivatives", description: null },
    ],
    edges: [{ prereq_kc_id: "k-2", kc_id: "k-1", weight: 1.0 }],
    items: [
      {
        id: "i-keep",
        item_type: "mcq",
        stem: "The kept question",
        answer_key: { choices: ["a", "b"], correct: 0 },
        difficulty: 0,
        rubric_id: null,
        origin: "learner",
        kc_weights: [{ kc_id: "k-1", weight: 1 }],
      },
      {
        id: "i-strike",
        item_type: "mcq",
        stem: "The struck question",
        answer_key: { choices: ["c", "d"], correct: 1 },
        difficulty: 0,
        rubric_id: null,
        origin: "learner",
        kc_weights: [{ kc_id: "k-1", weight: 1 }],
      },
    ],
    rubrics: [],
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function serve(publications: unknown[] = [PUBLICATION]) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : null;
    if (request?.method === "POST") return Promise.resolve(jsonResponse({ id: "s-1" }));
    return Promise.resolve(jsonResponse({ publications }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PublicationQueue />
    </QueryClientProvider>,
  );
}

async function postBody(fetchMock: ReturnType<typeof serve>): Promise<string> {
  const request = await waitFor(() => {
    const found = fetchMock.mock.calls
      .map(([input]) => input)
      .find((input): input is Request => input instanceof Request && input.method === "POST");
    expect(found).toBeDefined();
    return found!;
  });
  return request.clone().text();
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("the review queue", () => {
  it("shows the graph and who asked", async () => {
    serve();
    renderQueue();

    expect(await screen.findByText("Calculus")).toBeInTheDocument();
    expect(screen.getByText("Integrals")).toBeInTheDocument();
    expect(screen.getByText("Substitution")).toBeInTheDocument();
    expect(screen.getByText(/Requested by author/)).toBeInTheDocument();
  });

  it("shows answer keys, because that is what is being approved", async () => {
    serve();
    renderQueue();

    expect(await screen.findByText(/"correct":0/)).toBeInTheDocument();
  });

  it("sends the items the reviewer unticked", async () => {
    const fetchMock = serve();
    renderQueue();

    fireEvent.click(await screen.findByLabelText("Include: The struck question"));
    fireEvent.click(screen.getByRole("button", { name: "Approve and share" }));

    const body = await postBody(fetchMock);
    expect(body).toContain("i-strike");
    expect(body).not.toContain("i-keep");
  });

  it("will not reject without a note", async () => {
    serve();
    renderQueue();

    const reject = await screen.findByRole("button", { name: "Reject" });
    expect(reject).toBeDisabled();
    expect(screen.getByText("Rejecting needs a note.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Note to the author"), {
      target: { value: "Answer keys are wrong." },
    });
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("says plainly when there is nothing waiting", async () => {
    serve([]);
    renderQueue();

    expect(await screen.findByText("Nothing is waiting for review.")).toBeInTheDocument();
  });
});
