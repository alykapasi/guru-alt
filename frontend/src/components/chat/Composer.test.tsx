import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";

describe("Composer", () => {
  it("offers Stop instead of Send while a reply streams", async () => {
    const onStop = vi.fn();
    render(<Composer disabled streaming onStop={onStop} onSend={() => {}} />);
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("offers Send when nothing is streaming", () => {
    render(<Composer disabled={false} onSend={() => {}} />);
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });

  it("says which mode is on, and names the message box", () => {
    render(<Composer onSend={() => {}} disabled={false} />);
    expect(screen.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Agentic" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
  });
});
