import { describe, expect, it } from "vitest";
import { render, renderHook, screen } from "@testing-library/react";
import { LiveAnnouncer } from "./LiveAnnouncer";
import { gradeAnnouncement, useTurnAnnouncement } from "../hooks/useTurnAnnouncement";
import type { CheckResult } from "../api/sse";

const grade: CheckResult = { item_id: "i", score: 0.8, correct: true, components: [] };

describe("useTurnAnnouncement", () => {
  it("announces the start once, not each token", () => {
    const { result, rerender } = renderHook(({ busy }) => useTurnAnnouncement(busy, null), {
      initialProps: { busy: false },
    });
    expect(result.current).toBe("");
    rerender({ busy: true });
    expect(result.current).toBe("Guru is replying…");
    rerender({ busy: true });
    expect(result.current).toBe("Guru is replying…");
  });

  it("announces the end as finished, a grade, or the error", () => {
    const run = (error: string | null, g: CheckResult | null) => {
      const { result, rerender } = renderHook(({ busy }) => useTurnAnnouncement(busy, error, g), {
        initialProps: { busy: true },
      });
      rerender({ busy: false });
      return result.current;
    };
    expect(run(null, null)).toBe("Reply finished");
    expect(run(null, grade)).toBe("Marked correct, scored 80%");
    expect(run("The tutor is busy.", grade)).toBe("The tutor is busy.");
  });

  it("announces a practice notice when one appears", () => {
    const { result, rerender } = renderHook(
      ({ notice }) => useTurnAnnouncement(false, null, null, notice),
      { initialProps: { notice: null as string | null } },
    );
    rerender({ notice: "Practice paused" });
    expect(result.current).toBe("Practice paused");
  });
});

describe("gradeAnnouncement", () => {
  it("says a miss gently", () => {
    expect(gradeAnnouncement({ ...grade, correct: false, score: 0.4 })).toBe(
      "Marked, not quite yet, scored 40%",
    );
  });
});

describe("LiveAnnouncer", () => {
  it("is a polite status region", () => {
    render(<LiveAnnouncer message="Reply finished" />);
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Reply finished");
  });
});
