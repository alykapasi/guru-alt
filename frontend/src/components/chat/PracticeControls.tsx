/** The composer-adjacent controls for guided practice (S52) — a learner mid-question can pause
 * for a side discussion or skip the question outright, without either being read as a wrong
 * answer. `onPause` is optional: the plain-chat tutor's own checks (S15) reuse this component
 * with only `onSkip` — a tutor check is already conversational, so there is nothing a pause
 * would add there. */
export function PracticeControls({
  onPause,
  onSkip,
  disabled = false,
}: {
  onPause?: () => void;
  onSkip: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="mx-auto flex w-full max-w-3xl items-center justify-end gap-2 px-6 pb-2">
      {onPause && (
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={onPause}
          disabled={disabled}
        >
          Pause
        </button>
      )}
      <button type="button" className="btn btn-ghost btn-xs" onClick={onSkip} disabled={disabled}>
        Skip
      </button>
    </div>
  );
}
