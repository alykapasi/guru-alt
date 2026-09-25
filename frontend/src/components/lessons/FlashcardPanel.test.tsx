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
    // ...and which of the two ways to answer this card is the one that works. A flashcard
    // answered in prose is sent straight back to be rated and writes nothing to the
    // transcript, so typing at it looks exactly like the app having frozen.
    expect(screen.getByText(/answered by rating, not by typing/i)).toBeInTheDocument();
  });

  it("reports the rating the learner chose", async () => {
    const onRate = vi.fn();
    render(<FlashcardPanel item={item} onRate={onRate} reveal={async () => "cos x"} />);
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    await userEvent.click(screen.getByRole("button", { name: /^good$/i }));
    expect(onRate).toHaveBeenCalledWith(3);
  });

  it("still lets the learner rate when the answer cannot be revealed", async () => {
    // A flashcard whose `answer_key` is null is a real, reachable card: `_parse_flashcard`
    // treats `answer` as optional and POST /items accepts one without a key, so the reveal
    // endpoint 422s. With the rejection unhandled and the ratings gated on a non-null answer,
    // the learner was stuck: nothing rendered, no way to rate, and the workflow re-asked the
    // same card indefinitely. A rating only reports whether the memory came back, which they
    // can answer without us.
    const onRate = vi.fn();
    render(
      <FlashcardPanel
        item={item}
        onRate={onRate}
        reveal={async () => {
          throw new Error("reveal failed: 422 Unprocessable Content");
        }}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^good$/i }));
    expect(onRate).toHaveBeenCalledWith(3);
  });

  it("disables the ratings while a turn is in flight", async () => {
    // `send` early-returns on `pending`, so a click landing then is dropped without a trace.
    render(<FlashcardPanel item={item} onRate={vi.fn()} reveal={async () => "cos x"} disabled />);
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    expect(await screen.findByRole("button", { name: /^good$/i })).toBeDisabled();
  });
});
