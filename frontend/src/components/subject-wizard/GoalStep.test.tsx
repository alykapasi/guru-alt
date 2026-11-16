import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { GoalStep } from "./GoalStep";

const refinement = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
vi.mock("../../api/onboarding", () => ({ useGoalRefinement: () => refinement.state }));

function renderStep(proposal: string | null) {
  refinement.state = {
    proposal,
    committedGoal: null,
    isStreaming: false,
    error: null,
    tokens: "",
    send: vi.fn(),
  };
  return render(<GoalStep sessionId="g-1" onGoalCommitted={vi.fn()} />);
}

describe("the goal step", () => {
  it("asks once, with an example, and nothing repeating the heading", () => {
    renderStep(null);
    expect(screen.getByRole("textbox", { name: "Your goal" })).toHaveAttribute(
      "placeholder",
      expect.stringMatching(/^E\.g\./),
    );
    expect(screen.queryByText(/Share your goal/)).toBeNull();
  });

  it("asks for changes once there is a proposal to accept", () => {
    renderStep("Understand eigenvalues well enough to diagonalise a 3×3 matrix.");
    expect(screen.getByRole("textbox", { name: "Your goal" })).toHaveAttribute(
      "placeholder",
      "Or say what to change…",
    );
    expect(screen.getByRole("button", { name: "Looks good" })).toBeInTheDocument();
  });
});
