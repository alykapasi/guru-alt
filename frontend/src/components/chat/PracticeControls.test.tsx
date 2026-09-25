import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PracticeControls } from "./PracticeControls";

describe("PracticeControls", () => {
  it("pauses and skips", async () => {
    const onPause = vi.fn();
    const onSkip = vi.fn();
    render(<PracticeControls onPause={onPause} onSkip={onSkip} />);
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(onPause).toHaveBeenCalledOnce();
    expect(onSkip).toHaveBeenCalledOnce();
  });

  it("offers only skip where pausing means nothing", () => {
    render(<PracticeControls onSkip={() => {}} />);
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.getByRole("button", { name: "Skip" })).toBeInTheDocument();
  });
});
