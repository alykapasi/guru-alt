/** Shown in place of the composer-adjacent controls while guided practice is paused for a side
 * discussion (S52) — the learner went off to ask something else, and this is the way back to
 * the question, or out of it. `notice` carries the one case resuming can itself report: the
 * paused question went stale (the plan moved on, or the learner mastered it elsewhere) while it
 * waited, so there is nothing left to go back to. It renders in the same spot the strip always
 * has, rather than vanishing, so the learner sees why nothing came back rather than just having
 * the controls disappear. */
export function PracticePausedStrip({
  onResume,
  onSkip,
  disabled = false,
  notice = null,
}: {
  onResume: () => void;
  onSkip: () => void;
  disabled?: boolean;
  notice?: string | null;
}) {
  return (
    <div className="border-base-300 mx-auto flex w-full max-w-3xl flex-col gap-1 border-t px-6 pt-3 pb-2">
      <div className="flex items-center justify-between gap-3">
        <p className="text-caption text-base-content/60">Practice paused</p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={onResume}
            disabled={disabled}
          >
            Back to the question
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={onSkip}
            disabled={disabled}
          >
            Skip it
          </button>
        </div>
      </div>
      {notice && <p className="text-caption text-base-content/60">{notice}</p>}
    </div>
  );
}
