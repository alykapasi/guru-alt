import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { TurnError } from "./TurnError";

describe("TurnError", () => {
  afterEach(() => vi.useRealTimers());

  it("waits out a busy provider before offering to try again", () => {
    vi.useFakeTimers();
    render(<TurnError error="The tutor is busy" canRetry retryAfter={3} onRetry={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Try again in 3s" })).toBeDisabled();
    for (let i = 0; i < 3; i++) act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("offers to try again at once when there is nothing to wait for", () => {
    const onRetry = vi.fn();
    render(<TurnError error="Unreachable" canRetry retryAfter={null} onRetry={onRetry} />);

    screen.getByRole("button", { name: "Try again" }).click();
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
