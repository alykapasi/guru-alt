import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { CitationBody, CitationPane } from "./CitationPane";

vi.mock("../../api/hooks", () => ({
  useChunk: () => ({
    data: { text: "a passage", provenance: {}, superseded: false, reading_note: null },
    isLoading: false,
    isError: false,
  }),
  useSource: () => ({ data: { origin: "notes.pdf" }, isLoading: false }),
}));

const citation = { marker: 1, chunk_id: "k1", source_id: "s1" };

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

describe("CitationBody reading note", () => {
  it("shows how a passage was read", () => {
    render(
      <CitationBody
        origin="scan.pdf"
        text="blurry words"
        note="read from a scan or image; wording may contain errors"
      />,
    );
    expect(
      screen.getByText("Read from a scan or image; wording may contain errors"),
    ).toBeInTheDocument();
  });
});

describe("CitationPane focus", () => {
  it("takes focus when it opens and gives it back when it closes", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<CitationPane citation={citation} onClose={() => {}} />);
    expect(screen.getByRole("heading", { name: "Source" })).toHaveFocus();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("names its close button", () => {
    render(<CitationPane citation={citation} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: "Close source" })).toBeInTheDocument();
  });
});
