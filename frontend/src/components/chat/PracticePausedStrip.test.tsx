import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PracticePausedStrip } from "./PracticePausedStrip";

describe("PracticePausedStrip", () => {
  it("offers the way back and the way out", async () => {
    const onResume = vi.fn();
    const onSkip = vi.fn();
    render(<PracticePausedStrip onResume={onResume} onSkip={onSkip} />);
    expect(screen.getByText("Practice paused")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back to the question" }));
    await userEvent.click(screen.getByRole("button", { name: "Skip it" }));
    expect(onResume).toHaveBeenCalledOnce();
    expect(onSkip).toHaveBeenCalledOnce();
  });

  it("says when the question no longer fits", () => {
    render(
      <PracticePausedStrip
        onResume={() => {}}
        onSkip={() => {}}
        notice="That question no longer fits your plan, so practice ended."
      />,
    );
    expect(
      screen.getByText("That question no longer fits your plan, so practice ended."),
    ).toBeInTheDocument();
  });

  it("shows an ended notice on its own, not as a pause", () => {
    render(
      <PracticePausedStrip
        onResume={() => {}}
        onSkip={() => {}}
        notice="That question no longer fits your plan, so practice ended."
      />,
    );
    expect(screen.queryByText("Practice paused")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("goes quiet while busy", () => {
    render(<PracticePausedStrip onResume={() => {}} onSkip={() => {}} disabled />);
    expect(screen.getByRole("button", { name: "Back to the question" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Skip it" })).toBeDisabled();
  });
});
