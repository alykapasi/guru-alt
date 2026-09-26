import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceSettingsPanel } from "./SourceSettingsPanel";

describe("SourceSettingsPanel", () => {
  it("shows both switches in their current state", () => {
    render(<SourceSettingsPanel includeUntagged={false} sourcesOnly onChange={() => {}} />);
    expect(screen.getByRole("checkbox", { name: /untagged materials/i })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /only from my sources/i })).toBeChecked();
  });

  it("reports one switch at a time", async () => {
    const onChange = vi.fn();
    render(<SourceSettingsPanel includeUntagged={false} sourcesOnly={false} onChange={onChange} />);
    await userEvent.click(screen.getByRole("checkbox", { name: /untagged materials/i }));
    expect(onChange).toHaveBeenCalledWith({ include_untagged_sources: true });
  });
});
