import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialsStep } from "./MaterialsStep";
import { stillIngesting } from "../../api/hooks";

/** Grounding a curriculum in the learner's own documents is the reason uploads exist, and this
 * step is the only place in the product where that choice is offered.
 *
 * These stub `fetch` rather than the hook, deliberately. The defect that prompted them was not
 * in the rendering at all — the step called a hook that disables itself when given no subject,
 * so the request was never made, and it rendered neither the list nor its own empty-state
 * message. Mocking the hook would have made that test pass while the product stayed broken.
 */

const SOURCE = {
  id: "s-1",
  kind: "file",
  origin: "photosynthesis-notes.txt",
  content_type: "text/plain",
  status: "done",
  error: null,
  subject_id: null,
  topic_id: null,
  created_at: "2026-09-01T00:00:00Z",
};

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function renderStep() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MaterialsStep value={[]} onChange={() => {}} onNext={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("choosing material to build a subject from", () => {
  it("offers an uploaded document to select", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse([SOURCE]))),
    );

    renderStep();

    expect(await screen.findByText("photosynthesis-notes.txt")).toBeInTheDocument();
    expect(await screen.findByRole("checkbox")).toBeInTheDocument();
  });

  it("actually asks the server, rather than rendering a step that never loads", async () => {
    const fetcher = vi.fn(() => Promise.resolve(jsonResponse([])));
    vi.stubGlobal("fetch", fetcher);

    renderStep();

    // The specific failure this pins: a query that is disabled makes no request, and a step
    // showing nothing is indistinguishable from a learner who has uploaded nothing.
    await waitFor(() => expect(fetcher).toHaveBeenCalled());
  });

  it("says there is nothing yet, instead of showing an empty gap", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    );

    renderStep();

    expect(await screen.findByText(/No materials yet/)).toBeInTheDocument();
  });
});

describe("knowing when to keep asking", () => {
  it("keeps watching while a document is still being worked on", () => {
    expect(stillIngesting([{ status: "pending" }])).toBe(true);
    expect(stillIngesting([{ status: "processing" }])).toBe(true);
    expect(stillIngesting([{ status: "done" }, { status: "pending" }])).toBe(true);
  });

  it("stops once nothing is in flight, so a settled library costs no requests", () => {
    expect(stillIngesting([{ status: "done" }])).toBe(false);
    expect(stillIngesting([{ status: "failed" }])).toBe(false);
    expect(stillIngesting([])).toBe(false);
    expect(stillIngesting(undefined)).toBe(false);
  });
});
