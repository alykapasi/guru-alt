import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GuidanceToggle } from "./GuidanceToggle";

describe("GuidanceToggle", () => {
  it("shows the current mode and what it means", () => {
    render(<GuidanceToggle value="exploration" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "Exploration" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByText("Guru offers detours; you decide.")).toBeInTheDocument();
  });

  it("reports a change", async () => {
    const onChange = vi.fn();
    render(<GuidanceToggle value="guided" onChange={onChange} />);
    await userEvent.click(screen.getByRole("radio", { name: "Exploration" }));
    expect(onChange).toHaveBeenCalledWith("exploration");
  });
});
