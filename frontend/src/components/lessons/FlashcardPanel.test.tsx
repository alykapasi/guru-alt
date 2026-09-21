import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FlashcardPanel } from "./FlashcardPanel";

const item = {
  id: "11111111-1111-1111-1111-111111111111",
  item_type: "flashcard",
  stem: "What is the derivative of sin x?",
  difficulty: 0.5,
  rubric_id: null,
  kcs: [],
};

describe("FlashcardPanel", () => {
  it("withholds the answer and the ratings until the learner reveals", () => {
    render(<FlashcardPanel item={item} onRate={vi.fn()} />);
    expect(screen.getByRole("button", { name: /reveal/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^good$/i })).not.toBeInTheDocument();
  });

  it("says what a rating actually does", async () => {
    render(<FlashcardPanel item={item} onRate={vi.fn()} reveal={async () => "cos x"} />);
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    expect(await screen.findByText("cos x")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^good$/i })).toBeInTheDocument();
    // The learner is told which of the two things their click moves. Without this line the
    // rating reads as a score, which is the misunderstanding this whole slice is about.
    expect(screen.getByText(/sets when this comes back/i)).toBeInTheDocument();
  });

  it("reports the rating the learner chose", async () => {
    const onRate = vi.fn();
    render(<FlashcardPanel item={item} onRate={onRate} reveal={async () => "cos x"} />);
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    await userEvent.click(screen.getByRole("button", { name: /^good$/i }));
    expect(onRate).toHaveBeenCalledWith(3);
  });
});
