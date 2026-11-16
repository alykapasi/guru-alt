import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const setPref = vi.fn();
let rows: unknown[] = [];
vi.mock("../../api/hooks", () => ({
  usePreferences: () => ({ data: rows }),
  useSetPreference: () => ({ mutate: setPref, isPending: false, error: null }),
}));

import { PreferenceControls } from "./PreferenceControls";

const pace = (over: object) => ({
  key: "pace",
  label: "Pace",
  value: "auto",
  source: "default",
  options: ["auto", "brisk", "standard", "unhurried"],
  global_value: null,
  inferred: "brisk",
  ...over,
});

describe("PreferenceControls", () => {
  beforeEach(() => setPref.mockClear());

  it("shows what adaptation currently chose when set to adapt", () => {
    rows = [pace({})];
    render(<PreferenceControls />);
    expect(screen.getByText(/Adapting to you: brisk/i)).toBeInTheDocument();
  });

  it("pins a value when chosen", () => {
    rows = [pace({})];
    render(<PreferenceControls />);
    fireEvent.change(screen.getByLabelText("Pace"), { target: { value: "unhurried" } });
    expect(setPref).toHaveBeenCalledWith({ key: "pace", value: "unhurried" });
  });

  it("per subject, says once that settings follow the defaults, not under every setting", () => {
    rows = [
      pace({ value: "brisk", source: "global", global_value: "brisk" }),
      pace({ key: "hints", label: "Hints", value: "auto", source: "default", inferred: null }),
    ];
    render(<PreferenceControls subjectId="s1" />);
    expect(screen.getAllByText(/follow your defaults/i)).toHaveLength(1);
    expect(screen.queryByText(/Using your default/i)).toBeNull();
  });

  it("per subject, marks a changed setting and offers to go back to the default", () => {
    rows = [pace({ value: "unhurried", source: "subject", global_value: "brisk" })];
    render(<PreferenceControls subjectId="s1" />);
    expect(
      screen.getByText(/Changed for this subject — your default is Brisk/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Use my default for Pace/i }));
    expect(setPref).toHaveBeenCalledWith({ key: "pace", value: null });
  });

  it("does not say settings follow defaults on the defaults themselves", () => {
    rows = [pace({})];
    render(<PreferenceControls />);
    expect(screen.queryByText(/follow your defaults/i)).toBeNull();
  });

  it("explains what each guidance mode does", () => {
    rows = [
      {
        key: "guidance",
        label: "Guidance",
        value: "exploration",
        source: "global",
        options: ["guided", "exploration"],
        global_value: null,
        inferred: null,
      },
    ];
    render(<PreferenceControls />);
    expect(screen.getByText(/Guru offers detours; you decide/)).toBeInTheDocument();
  });
});
