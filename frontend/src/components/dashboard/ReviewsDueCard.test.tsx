import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const now = new Date().toISOString();
vi.mock("../../api/hooks", () => ({
  useReviewsDue: () => ({
    data: [
      { kc_id: "kc-1", due_at: now, ability: 0, uncertainty: 1, kind: "review", item: null },
      {
        kc_id: "kc-2",
        due_at: now,
        ability: 0,
        uncertainty: 1,
        kind: "retention_check",
        item: null,
      },
    ],
  }),
  useKC: (id: string) => ({ data: { name: id === "kc-1" ? "Vectors" : "Matrices" } }),
}));

import { ReviewsDueCard } from "./ReviewsDueCard";

/** A retention check is a review that must be answered unaided, so the queue says which rows
 * are checks (S14). */
describe("the review queue", () => {
  it("labels a retention check and only that row", () => {
    render(<ReviewsDueCard />);
    expect(screen.getAllByText("Retention check")).toHaveLength(1);
    expect(screen.getByText("Matrices").closest("div")).toHaveTextContent("Retention check");
  });
});
