import { describe, expect, it } from "vitest";
import type { components } from "../../api/schema";
import { duplicateLabel } from "./duplicateLabel";

type Source = components["schemas"]["SourceRead"];

function source(id: string, origin: string, duplicateOf: string | null = null): Source {
  return {
    id,
    origin,
    kind: "file",
    content_type: "text/plain",
    status: "done",
    error: null,
    subject_id: null,
    topic_id: null,
    duplicate_of_id: duplicateOf,
    created_at: "2026-09-26T00:00:00Z",
  };
}

describe("duplicateLabel", () => {
  it("names the original when it is in the list", () => {
    const original = source("a", "uk.txt");
    const dup = source("b", "us.txt", "a");
    expect(duplicateLabel(dup, [original, dup])).toBe("Same text as uk.txt");
  });

  it("still says so when the original is not in the list", () => {
    const dup = source("b", "us.txt", "gone");
    expect(duplicateLabel(dup, [dup])).toBe("Same text as another of your sources");
  });

  it("says nothing for an ordinary source", () => {
    const src = source("a", "uk.txt");
    expect(duplicateLabel(src, [src])).toBeNull();
  });
});
