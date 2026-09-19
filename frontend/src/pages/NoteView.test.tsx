import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { NoteView } from "./NoteView";

const authored = "# 私の notes 🧠\n\n```unclosed\nliteral";
const generated = "## Automatic additions\n\n**New learning**";
const note = {
  topic_id: "topic",
  content_md: authored + "\n\n" + generated,
  learner_authored_md: authored,
  generated_md: generated,
  format: null,
  effective_format: "outline",
  stale: false,
  revision_ordinal: 3,
  updated_at: null,
};
function renderPage() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={["/notes/topic"]}>
        <Routes>
          <Route path="/notes/:topicId" element={<NoteView />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => vi.restoreAllMocks());
it("keeps automatic additions outside an unclosed learner code fence", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(note), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    ),
  );
  renderPage();
  expect(await screen.findByRole("heading", { name: "Automatic additions" })).toBeInTheDocument();
  expect(screen.getByText("New learning").tagName).toBe("STRONG");
});
it("edits exact authored text and only adopts additions explicitly", async () => {
  const fetchMock = vi.fn((input: Request | string, init?: RequestInit) => {
    void input;
    void init;
    return Promise.resolve(
      new Response(JSON.stringify(note), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  expect(screen.getByRole("textbox")).toHaveValue(authored);
  await userEvent.click(screen.getByRole("button", { name: "Include additions to edit them" }));
  expect(screen.getByRole("textbox")).toHaveValue(authored + "\n\n" + generated);
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true),
  );
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")![1]!;
  expect(JSON.parse(request.body as string)).toEqual({
    content_md: authored + "\n\n" + generated,
    include_generated: true,
    expected_revision_ordinal: 3,
  });
});
