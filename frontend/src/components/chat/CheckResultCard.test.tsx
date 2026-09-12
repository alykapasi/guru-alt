import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CheckResultCard } from "./CheckResultCard";
import type { CheckComponent, CheckResult } from "../../api/sse";

/** The rule these hold is that absent evidence is shown as absent. A grade the learner can
 * read is only worth having if it does not assert more than the grader actually said. */

function component(overrides: Partial<CheckComponent> = {}): CheckComponent {
  return {
    kc_id: "kc-1",
    kc_name: "Velocity",
    score: null,
    prior_ability: 0,
    ability: 0,
    uncertainty: 0.9,
    failure_kind: null,
    failure_detail: null,
    ...overrides,
  };
}

function result(overrides: Partial<CheckResult> = {}): CheckResult {
  return {
    item_id: "item-1",
    score: 0.9,
    correct: true,
    components: [component()],
    ...overrides,
  };
}

describe("the graded-answer card", () => {
  it("says what the answer scored and whether it was marked correct", () => {
    render(<CheckResultCard result={result({ score: 0.9, correct: true })} />);
    expect(screen.getByText(/Marked correct/)).toBeInTheDocument();
    expect(screen.getByText(/scored 90%/)).toBeInTheDocument();
  });

  it("does not call a wrong answer correct", () => {
    render(<CheckResultCard result={result({ score: 0.2, correct: false })} />);
    expect(screen.getByText(/not quite yet/)).toBeInTheDocument();
    expect(screen.queryByText(/Marked correct/)).not.toBeInTheDocument();
  });

  it("shows a component's own score only when the grader produced one", () => {
    const { rerender } = render(
      <CheckResultCard result={result({ components: [component({ score: null })] })} />,
    );
    expect(screen.queryByText(/on this part/)).not.toBeInTheDocument();

    rerender(<CheckResultCard result={result({ components: [component({ score: 0.4 })] })} />);
    expect(screen.getByText(/40% on this part/)).toBeInTheDocument();
  });

  it("shows the movement in both directions, and says so when nothing moved", () => {
    const { rerender } = render(
      <CheckResultCard
        result={result({ components: [component({ prior_ability: 0, ability: 1 })] })}
      />,
    );
    expect(screen.getByText(/50% → 73%/)).toBeInTheDocument();

    rerender(
      <CheckResultCard
        result={result({ components: [component({ prior_ability: 1, ability: 0 })] })}
      />,
    );
    expect(screen.getByText(/73% → 50%/)).toBeInTheDocument();

    rerender(
      <CheckResultCard
        result={result({ components: [component({ prior_ability: 0.5, ability: 0.5 })] })}
      />,
    );
    expect(screen.getByText(/unchanged at 62%/)).toBeInTheDocument();
  });

  it("gives no reason when nothing diagnosed one", () => {
    render(
      <CheckResultCard result={result({ components: [component({ failure_kind: null })] })} />,
    );
    expect(screen.queryByText(/needs another look/)).not.toBeInTheDocument();
    expect(screen.queryByText(/getting in the way/)).not.toBeInTheDocument();
  });

  it("puts a diagnosis in the learner's terms, with the grader's own sentence", () => {
    render(
      <CheckResultCard
        result={result({
          components: [
            component({
              failure_kind: "procedural",
              failure_detail: "The method was right; a sign was dropped.",
            }),
          ],
        })}
      />,
    );
    expect(
      screen.getByText(/Right method, slip along the way — The method was right/),
    ).toBeInTheDocument();
  });

  it("says what the percentages mean, because they are not what they look like", () => {
    // Under this estimator sigmoid(ability) is expected score on an average question, not the
    // share of a topic understood (S46). Showing a bare percentage invites the other reading.
    render(<CheckResultCard result={result()} />);
    expect(screen.getByText(/not how much of the topic you know/)).toBeInTheDocument();
  });

  it("names every component, not just the one that failed", () => {
    render(
      <CheckResultCard
        result={result({
          components: [
            component({ kc_id: "a", kc_name: "Projection", score: 1 }),
            component({ kc_id: "b", kc_name: "Least squares", score: 0 }),
          ],
        })}
      />,
    );
    expect(screen.getByText("Projection")).toBeInTheDocument();
    expect(screen.getByText("Least squares")).toBeInTheDocument();
  });
});
