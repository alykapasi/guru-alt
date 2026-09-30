import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../../api/hooks", () => ({
  useProfile: () => ({
    data: {
      dimensions: [
        { key: "pace", label: "Pace", observation: "Speeding up", value: { trend: "speeding_up" } },
        { key: "engagement", label: "Engagement", observation: null, value: 0.5 },
        { key: "interests", label: "Topics", observation: null, value: ["chess"], paused: true },
      ],
    },
  }),
  useRefreshProfile: () => ({ mutate: vi.fn(), isPending: false }),
  useResetDimension: () => ({ mutate: vi.fn() }),
  usePreferences: () => ({
    data: [
      {
        key: "pace",
        label: "Pace",
        value: "brisk",
        source: "global",
        options: ["auto", "brisk", "standard", "unhurried"],
        global_value: null,
        inferred: "brisk",
      },
    ],
  }),
}));

import { ProfileSection } from "./ProfileSection";

describe("ProfileSection", () => {
  it("says when the learner's own setting overrides an inferred dimension", () => {
    render(<ProfileSection />);
    expect(screen.getAllByText(/Your setting overrides this/)).toHaveLength(1);
  });

  it("says which dimensions wait while memory is paused", () => {
    render(<ProfileSection />);
    expect(screen.getAllByText(/Paused — not updated while/)).toHaveLength(1);
  });
});
