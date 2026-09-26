import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceActions } from "./SourceActions";

describe("SourceActions", () => {
  it("retries a failed source straight away", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="failed" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledWith(false);
  });

  it("asks before re-processing a finished source", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="done" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Re-process" }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.getByText(/already in your library/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Replace passages" }));
    expect(onRetry).toHaveBeenCalledWith(true);
  });

  it("lets the learner back out", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="done" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Re-process" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.queryByText(/already in your library/)).not.toBeInTheDocument();
  });

  it("offers nothing while a source is being processed", () => {
    const { container } = render(<SourceActions status="processing" onRetry={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
