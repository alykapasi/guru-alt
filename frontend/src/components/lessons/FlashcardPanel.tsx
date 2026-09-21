import { useState } from "react";
import { RichText } from "../content/RichText";
import type { ItemEvent } from "../../api/sse";
import { defaultReveal } from "../../api/hooks";

/** FSRS's four grades, in the order the learner sees them. The numbers are the contract with
 * `grade_flashcard` (1=Again … 4=Easy) — see app/learning/grading.py's _RATING_SCORE. */
const RATINGS: { label: string; value: number }[] = [
  { label: "Again", value: 1 },
  { label: "Hard", value: 2 },
  { label: "Good", value: 3 },
  { label: "Easy", value: 4 },
];

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
}: {
  item: ItemEvent;
  onRate: (rating: number) => void;
  reveal?: (itemId: string) => Promise<string>;
}) {
  const [back, setBack] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onReveal() {
    setBusy(true);
    try {
      const fetcher = reveal ?? defaultReveal;
      setBack(await fetcher(item.id));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <RichText content={item.stem} className="text-base-content/90" />
      {back === null ? (
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
          <div className="border-base-300 rounded-box border p-3">
            <RichText content={back} className="text-base-content/90" />
          </div>
          <p className="text-caption text-base-content/60">How did you do?</p>
          <div className="flex flex-wrap gap-2">
            {RATINGS.map((r) => (
              <button
                key={r.value}
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => onRate(r.value)}
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
