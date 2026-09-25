import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { GoalStatusBar } from "./GoalStatusBar";
import type { components } from "../../api/schema";

type GoalStatus = components["schemas"]["GoalStatusRead"];

const status = (over: Partial<GoalStatus> = {}): GoalStatus => ({
  objective_kc_count: 10,
  achieved_kc_count: 4,
  current_kc_count: 4,
  stale_kc_count: 0,
  achieved_at: null,
  closed_at: null,
  ...over,
});

describe("GoalStatusBar", () => {
  it("says how much of the goal is not in the current window", () => {
    render(<GoalStatusBar status={status()} deferredCount={6} />);
    expect(screen.getByText(/6 more components/i)).toBeInTheDocument();
  });

  it("renders nothing when the objective is unknown", () => {
    // 0 means "this plan predates objectives", not "nothing left to do". A full-looking
    // empty bar is the most misleading thing this panel could show.
    const { container } = render(
      <GoalStatusBar status={status({ objective_kc_count: 0 })} deferredCount={0} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("reports stale evidence alongside an achievement, not instead of it", () => {
    render(
      <GoalStatusBar
        status={status({
          achieved_kc_count: 10,
          current_kc_count: 7,
          stale_kc_count: 3,
          achieved_at: "2026-03-01T00:00:00Z",
        })}
        deferredCount={0}
      />,
    );
    // "Goal complete", not just "demonstrated across independent checks": the in-progress
    // headline says that too, so matching it alone would pass with the achievement dropped.
    expect(screen.getByText(/goal complete/i)).toBeInTheDocument();
    expect(screen.getByText(/3 components? .*worth checking again/i)).toBeInTheDocument();
  });

  it("leads with the learner's closure", () => {
    render(
      <GoalStatusBar status={status({ closed_at: "2026-05-01T00:00:00Z" })} deferredCount={6} />,
    );
    expect(screen.getByText(/you closed this goal/i)).toBeInTheDocument();
    expect(screen.queryByText(/4 of 10 components/i)).not.toBeInTheDocument();
  });
});
