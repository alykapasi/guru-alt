import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MasteryEvidence } from "./MasteryEvidence";
import type { components } from "../../api/schema";

type KCMastery = components["schemas"]["KCMasteryRead"];

/** The same percentage can come from four different problems solved unaided over weeks, or
 * from one question answered four times in ten minutes. These hold that the display does not
 * let those look the same (S14). */

function kc(overrides: Partial<KCMastery> = {}): KCMastery {
  return {
    kc_id: "kc-1",
    kc_name: "Least squares",
    ability: 1,
    uncertainty: 0.4,
    mastered: false,
    assessed: true,
    distinct_items: 1,
    unassisted_items: 1,
    transfer_shown: false,
    retention_shown: false,
    ...overrides,
  } as KCMastery;
}

describe("the evidence behind a mastery estimate", () => {
  it("says nothing at all for a component with no evidence", () => {
    const { container } = render(<MasteryEvidence kc={kc({ assessed: false })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("counts the problems and how many were unaided", () => {
    render(<MasteryEvidence kc={kc({ distinct_items: 4, unassisted_items: 3 })} />);
    expect(screen.getByText(/4 problems, 3 unaided/)).toBeInTheDocument();
  });

  it("uses the singular for one problem", () => {
    render(<MasteryEvidence kc={kc({ distinct_items: 1 })} />);
    expect(screen.getByText(/1 problem,/)).toBeInTheDocument();
    expect(screen.queryByText(/1 problems/)).not.toBeInTheDocument();
  });

  it("states the absence of transfer rather than omitting it", () => {
    // "No different problem yet" and silence look identical on screen, and only one is true.
    render(<MasteryEvidence kc={kc({ transfer_shown: false })} />);
    expect(screen.getByText("no different problem yet")).toBeInTheDocument();
  });

  it("states the absence of a delayed check rather than omitting it", () => {
    render(<MasteryEvidence kc={kc({ retention_shown: false })} />);
    expect(screen.getByText("no delayed check yet")).toBeInTheDocument();
  });

  it("says when transfer and retention have actually been shown", () => {
    render(<MasteryEvidence kc={kc({ transfer_shown: true, retention_shown: true })} />);
    expect(screen.getByText("solved a different one")).toBeInTheDocument();
    expect(screen.getByText("held up later")).toBeInTheDocument();
    expect(screen.queryByText(/no delayed check/)).not.toBeInTheDocument();
  });

  it("still shows the thin evidence behind a component marked mastered", () => {
    // This is the case the counts exist for: "mastered" is doing the most work exactly where
    // the evidence is weakest, so it must not be the only thing on the row.
    render(
      <MasteryEvidence kc={kc({ mastered: true, distinct_items: 1, transfer_shown: false })} />,
    );
    expect(screen.getByText(/1 problem,/)).toBeInTheDocument();
    expect(screen.getByText("no different problem yet")).toBeInTheDocument();
  });
});
