import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { ChatShell } from "./ChatShell";
import { setViewportWide } from "../../test/viewport";

vi.mock("../NavBar", () => ({ NavBar: () => null }));
vi.mock("../ImpersonationBanner", () => ({ ImpersonationBanner: () => null }));
vi.mock("./ConversationSidebar", () => ({
  ConversationSidebar: () => <p>conversation list</p>,
}));

function Go() {
  const navigate = useNavigate();
  return <button onClick={() => navigate("/app/chat/abc")}>go</button>;
}

function shell() {
  return render(
    <MemoryRouter initialEntries={["/app/chat"]}>
      <Routes>
        <Route path="/app/chat" element={<ChatShell />}>
          <Route index element={<Go />} />
          <Route path=":conversationId" element={<p>conversation</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("ChatShell", () => {
  it("shows the conversation list as a column when wide, with no drawer button", () => {
    setViewportWide(true);
    shell();
    expect(screen.getByRole("complementary", { name: "Conversations" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Conversations" })).toBeNull();
  });

  it("puts the list in a drawer when narrow, closed until asked for", () => {
    shell();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Conversations" }));
    expect(screen.getByRole("dialog", { name: "Conversations" })).toHaveAttribute("data-modal");
  });

  it("closes the drawer when the route changes", () => {
    shell();
    fireEvent.click(screen.getByRole("button", { name: "Conversations" }));
    fireEvent.click(screen.getByRole("button", { name: "go" }));
    expect(screen.getByText("conversation")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("offers a skip link to the content", () => {
    shell();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
  });
});
