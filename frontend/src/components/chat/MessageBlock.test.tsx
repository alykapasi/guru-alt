import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageBlock } from "./MessageBlock";

describe("MessageBlock", () => {
  it("marks a reply the learner stopped", () => {
    render(<MessageBlock role="assistant" content="Half an answer" interrupted="stopped" />);
    expect(screen.getByText("Stopped")).toBeTruthy();
  });

  it("marks a reply cut off by the deadline", () => {
    render(<MessageBlock role="assistant" content="Slow start" interrupted="timed_out" />);
    expect(screen.getByText("This reply took too long and was cut off.")).toBeTruthy();
  });

  it("says nothing about a whole reply", () => {
    render(<MessageBlock role="assistant" content="A whole reply." />);
    expect(screen.queryByText("Stopped")).toBeNull();
  });
});
