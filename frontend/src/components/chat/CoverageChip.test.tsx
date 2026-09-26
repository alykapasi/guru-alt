import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CoverageChip } from "./CoverageChip";

describe("CoverageChip", () => {
  it("counts the sources a reply cited", () => {
    render(<CoverageChip coverage="cited" sourceCount={2} />);
    expect(screen.getByText("Draws on 2 of your sources")).toBeInTheDocument();
  });

  it("reads naturally for a single source", () => {
    render(<CoverageChip coverage="cited" sourceCount={1} />);
    expect(screen.getByText("Draws on one of your sources")).toBeInTheDocument();
  });

  it("says when materials were searched but not used", () => {
    render(<CoverageChip coverage="retrieved_not_cited" sourceCount={0} />);
    expect(screen.getByText("Your materials were searched but not used")).toBeInTheDocument();
  });

  it("says when nothing came from the learner's materials", () => {
    render(<CoverageChip coverage="none" sourceCount={0} />);
    expect(screen.getByText("Not from your materials")).toBeInTheDocument();
  });

  it("renders nothing without a scope", () => {
    const { container } = render(<CoverageChip coverage={null} sourceCount={0} />);
    expect(container).toBeEmptyDOMElement();
  });
});
