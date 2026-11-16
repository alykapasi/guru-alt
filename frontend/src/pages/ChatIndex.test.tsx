import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { ChatIndex } from "./ChatIndex";

/** The Chat tab resumes a chat. A practice session is the Lessons page's, and landing in one
 * from Chat put the learner in practice without their asking. */

function conversation(id: string, kind: string) {
  return { id, kind, title: null, subject_id: null, created_at: "2026-11-01T00:00:00Z" };
}

function Opened() {
  return <p>{`opened ${useParams().id}`}</p>;
}

function renderAt(conversations: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ conversations, has_more: false }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    ),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/app/chat"]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/app/chat" element={<ChatIndex />} />
          <Route path="/app/chat/:id" element={<Opened />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("opening the Chat tab", () => {
  it("skips a newer practice session and resumes the latest chat", async () => {
    renderAt([conversation("p-1", "session"), conversation("c-1", "chat")]);
    expect(await screen.findByText("opened c-1")).toBeInTheDocument();
  });

  it("does not open a practice session when there is no chat", async () => {
    renderAt([conversation("p-1", "session")]);
    expect(await screen.findByText(/Start a conversation/)).toBeInTheDocument();
    expect(screen.queryByText(/opened/)).toBeNull();
  });
});
