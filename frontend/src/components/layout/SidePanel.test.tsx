import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SidePanel } from "./SidePanel";
import { setViewportWide } from "../../test/viewport";

function element(open: boolean, onClose: () => void = () => {}) {
  return (
    <SidePanel open={open} onClose={onClose} side="right" label="Source" width="w-80">
      <p>passage</p>
    </SidePanel>
  );
}

describe("SidePanel, wide", () => {
  it("is a labelled column while open and absent while closed", () => {
    setViewportWide(true);
    const { rerender } = render(element(true));
    expect(screen.getByRole("complementary", { name: "Source" })).toHaveTextContent("passage");
    rerender(element(false));
    expect(screen.queryByRole("complementary")).toBeNull();
  });
});

describe("SidePanel, narrow", () => {
  it("opens as a modal dialog", () => {
    render(element(true));
    expect(screen.getByRole("dialog", { name: "Source" })).toHaveAttribute("data-modal", "true");
  });

  it("calls onClose on Escape, on its close button, and on the backdrop", () => {
    const onClose = vi.fn();
    render(element(true, onClose));
    const dialog = screen.getByRole("dialog", { name: "Source" });
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    fireEvent.click(screen.getByRole("button", { name: "Close Source" }));
    // A click whose target is the dialog itself landed on the backdrop.
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("closes the dialog when open turns false", () => {
    const { rerender } = render(element(true));
    rerender(element(false));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps its children mounted while closed", () => {
    render(element(false));
    expect(screen.getByText("passage", { selector: "p" })).toBeInTheDocument();
  });

  it("hides the close button when the content brings its own", () => {
    render(
      <SidePanel open onClose={() => {}} side="right" label="Source" width="w-80" showClose={false}>
        <p>passage</p>
      </SidePanel>,
    );
    expect(screen.queryByRole("button", { name: "Close Source" })).toBeNull();
  });
});

describe("SidePanel across the breakpoint", () => {
  it("does not leave a modal open after the viewport turns wide", () => {
    const { rerender } = render(element(true));
    setViewportWide(true);
    rerender(element(true));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("complementary", { name: "Source" })).toBeInTheDocument();
  });
});
