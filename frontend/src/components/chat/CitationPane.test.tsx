import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CitationBody } from "./CitationPane";

describe("CitationBody", () => {
  it("labels a passage from an earlier version of its source", () => {
    render(<CitationBody origin="notes.pdf" locator="Page 3" text="old words" superseded />);
    expect(screen.getByText("From an earlier version of this source")).toBeInTheDocument();
    expect(screen.getByText("old words")).toBeInTheDocument();
  });

  it("says plainly when the passage is gone", () => {
    render(<CitationBody missing />);
    expect(screen.getByText("This passage is no longer available.")).toBeInTheDocument();
    expect(screen.queryByText("Unknown source")).not.toBeInTheDocument();
  });

  it("shows a current passage without a label", () => {
    render(<CitationBody origin="notes.pdf" text="current words" />);
    expect(screen.getByText("current words")).toBeInTheDocument();
    expect(screen.queryByText(/earlier version/)).not.toBeInTheDocument();
  });
});
