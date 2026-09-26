import { describe, expect, it } from "vitest";
import { KEPT_COPY, describeCounts } from "./removalCopy";

describe("describeCounts", () => {
  it("names each non-zero count, singular and plural", () => {
    expect(describeCounts({ lessons: 1, cited_replies: 3 }, KEPT_COPY)).toEqual([
      "1 lesson built from this",
      "3 replies that cite this",
    ]);
  });

  it("leaves out what is zero", () => {
    expect(describeCounts({ lessons: 0, memories: 0 }, KEPT_COPY)).toEqual([]);
  });
});
