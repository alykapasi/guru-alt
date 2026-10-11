import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReviewExamples } from "./ReviewExamples";

/** Blind labelling is the point (S59): the model's decision must not be on screen until the
 * founder has saved their own label, or the agreement figures measure deference, not judgement. */

const VOCAB = {
  grading: ["band", ["fail", "partial", "pass"]],
  diagnosis: [
    "kind",
    ["none", "notation", "procedural", "conceptual", "prerequisite", "incomplete"],
  ],
  intent: ["intent", ["attempt", "deferral", "withdrawal"]],
  source_support: ["coverage", ["cited", "retrieved_not_cited", "none"]],
} as const;

const COUNTS = [
  { area: "grading", unlabelled: 0, labelled: 0, available: 3 },
  { area: "diagnosis", unlabelled: 0, labelled: 0, available: 0 },
  { area: "intent", unlabelled: 1, labelled: 2, available: 4 },
  { area: "source_support", unlabelled: 0, labelled: 0, available: 0 },
].map((c) => ({
  ...c,
  label_key: VOCAB[c.area as keyof typeof VOCAB][0],
  values: [...VOCAB[c.area as keyof typeof VOCAB][1]],
}));

const BLIND = {
  id: "11111111-1111-1111-1111-111111111111",
  area: "intent",
  snapshot: { stem: "What is a basis?", message: "Can you give a hint?" },
  created_at: "2026-10-11T00:00:00Z",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function serve({ next = BLIND as unknown }: { next?: unknown } = {}) {
  const calls: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      calls.push(request);
      const url = request.url;
      if (url.endsWith("/eval/areas")) return Promise.resolve(json(COUNTS));
      if (url.includes("/draw")) return Promise.resolve(json({ drawn: 3 }));
      if (url.includes("/next")) return Promise.resolve(json(next));
      if (url.includes("/labelled")) return Promise.resolve(json([]));
      if (request.method === "PUT") {
        return Promise.resolve(
          json({
            ...BLIND,
            decision: { intent: "deferral" },
            label: { intent: "deferral" },
            note: null,
            labelled_at: "2026-10-11T00:01:00Z",
          }),
        );
      }
      return Promise.resolve(json({}, 404));
    }),
  );
  return calls;
}

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReviewExamples />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("reviewing evaluation examples", () => {
  it("lists every area with what is waiting", async () => {
    serve();
    renderTab();
    const intent = await screen.findByRole("button", { name: /Intent/ });
    expect(intent).toHaveTextContent("1 to label");
    expect(intent).toHaveTextContent("2 labelled");
    expect(intent).toHaveTextContent("4 available");
  });

  it("shows the example without the model's decision until a label is saved", async () => {
    serve();
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: /Intent/ }));

    expect(await screen.findByText("Can you give a hint?")).toBeInTheDocument();
    expect(screen.queryByText(/Model decided/)).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: "deferral" }));
    fireEvent.click(screen.getByRole("button", { name: "Save label" }));

    expect(await screen.findByText(/Model decided: deferral/)).toBeInTheDocument();
  });

  it("sends the chosen label and note", async () => {
    const calls = serve();
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: /Intent/ }));
    fireEvent.click(await screen.findByRole("radio", { name: "attempt" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Note" }), {
      target: { value: "it was a try" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save label" }));

    const put = await waitFor(() => {
      const found = calls.find((c) => c.method === "PUT");
      expect(found).toBeDefined();
      return found!;
    });
    expect(JSON.parse(await put.clone().text())).toEqual({
      label: { intent: "attempt" },
      note: "it was a try",
    });
  });

  it("says how many were drawn", async () => {
    serve({ next: null });
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: /Grading/ }));
    expect(await screen.findByText("Nothing to label — draw some examples.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Draw 10" }));
    expect(await screen.findByText("Drew 3.")).toBeInTheDocument();
  });

  it("cannot save without a label", async () => {
    serve();
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: /Intent/ }));
    expect(await screen.findByRole("button", { name: "Save label" })).toBeDisabled();
  });
});
