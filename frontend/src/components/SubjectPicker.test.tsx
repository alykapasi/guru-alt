import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SubjectPicker } from "./SubjectPicker";

const subject = (id: string, name: string) =>
  ({ id, name, slug: id, owner_learner_id: "me" }) as never;

describe("SubjectPicker", () => {
  it("names a lone subject rather than offering a choice of one", () => {
    render(
      <SubjectPicker
        subjects={[subject("a", "Linear algebra")]}
        selectedId="a"
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Linear algebra")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers a choice, marking the chosen one, when there is more than one", () => {
    render(
      <SubjectPicker
        subjects={[subject("a", "Linear algebra"), subject("b", "Calculus")]}
        selectedId="b"
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Calculus" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Linear algebra" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("keeps the choice when an All option makes two", () => {
    render(
      <SubjectPicker
        subjects={[subject("a", "Linear algebra")]}
        selectedId={null}
        onSelect={vi.fn()}
        allLabel="All"
      />,
    );
    expect(screen.getAllByRole("button")).toHaveLength(2);
  });
});
