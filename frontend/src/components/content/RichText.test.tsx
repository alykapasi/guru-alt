import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { RichText } from "./RichText";

describe("the lazily loaded renderer", () => {
  it("shows the text immediately, then the rendered form", async () => {
    // The fallback matters during a stream: this component re-renders many times a second, and
    // a spinner there would mean the reply visibly vanishes before it typesets.
    render(<RichText content={"## Eigenvalues"} />);
    expect(screen.getByText("## Eigenvalues")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Eigenvalues" })).toBeInTheDocument();
  });
});
