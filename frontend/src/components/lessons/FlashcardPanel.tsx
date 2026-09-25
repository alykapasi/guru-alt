import { useState } from "react";
import { RichText } from "../content/RichText";
import type { ItemEvent } from "../../api/sse";
import { defaultReveal } from "../../api/hooks";
import { RATINGS } from "../../lib/flashcardRatings";

/** Think, reveal, then rate (S54).
 *
 * The answer is fetched on reveal rather than shipped with the question: withholding it in
 * the client would put it one devtools panel away, and a reveal you can skip is not a step.
 *
 * The line under the buttons is load-bearing, not decoration. A self-rating moves the review
 * schedule and deliberately does not move the mastery estimate (S56), and a learner who
 * thinks they are scoring themselves is being misled about what their click does.
 */
export function FlashcardPanel({
  item,
  onRate,
  reveal,
  disabled = false,
}: {
  item: ItemEvent;
  onRate: (rating: number) => void;
  reveal?: (itemId: string) => Promise<string>;
  /** A turn is in flight, so a rating would be dropped by the send hook — see Session.tsx. */
  disabled?: boolean;
}) {
  const [back, setBack] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(false);
  const [busy, setBusy] = useState(false);

  // `revealed` is tracked separately from `back` because a reveal can legitimately come back
  // with nothing: `generate_flashcard_item` stores `answer_key=None` when the model returned
  // no answer, and the reveal endpoint 422s on such a card. Gating the ratings on `back` alone
  // dead-ended that learner — the button re-enabled, nothing rendered, and the workflow handed
  // the same card back for a rating the UI gave them no way to give.
  async function onReveal() {
    setBusy(true);
    try {
      const fetcher = reveal ?? defaultReveal;
      setBack(await fetcher(item.id));
    } catch {
      setBack(null);
    } finally {
      setRevealed(true);
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <RichText content={item.stem} className="text-base-content/90" />
      {!revealed ? (
        <button
          type="button"
          className="btn btn-outline btn-sm self-start"
          onClick={onReveal}
          disabled={busy}
        >
          Reveal answer
        </button>
      ) : (
        <>
          {back !== null ? (
            <div className="border-base-300 rounded-box border p-3">
              <RichText content={back} className="text-base-content/90" />
            </div>
          ) : (
            // Rating on anyway is the honest recovery, not a fallback: a rating records
            // whether the memory came back, and the learner knows that whether or not we can
            // show them the card's other side.
            <p className="text-caption text-warning">
              The answer for this card could not be loaded — there may not be one stored. You can
              still say whether the memory came back; that is all a rating records.
            </p>
          )}
          {/* Answering in prose sends the card straight back to be rated and adds nothing to
              the transcript (see run_workflow_turn), so a learner who types gets silence. The
              copy has to say which of the two things this card wants. */}
          <p className="text-caption text-base-content/60">
            Rate your recall to move on — this card is answered by rating, not by typing.
          </p>
          <div className="flex flex-wrap gap-2">
            {RATINGS.map((r) => (
              <button
                key={r.value}
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => onRate(r.value)}
                disabled={disabled}
              >
                {r.label}
              </button>
            ))}
          </div>
          <p className="text-caption text-base-content/50">
            Sets when this comes back — not what Guru thinks you know.
          </p>
        </>
      )}
    </div>
  );
}
