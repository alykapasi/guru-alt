import { useEffect, useState } from "react";

/** A turn's error line and its Try again (S51). After a busy provider (S49) the button waits
 * out the time the provider asked for, counting down, rather than sending a retry that would
 * only be refused again. Mounted per error, so each error starts its own countdown. */
export function TurnError({
  error,
  canRetry,
  retryAfter,
  onRetry,
}: {
  error: string;
  canRetry: boolean;
  retryAfter: number | null;
  onRetry: () => void;
}) {
  const [remaining, setRemaining] = useState(() => Math.ceil(retryAfter ?? 0));
  useEffect(() => {
    if (remaining <= 0) return;
    const timer = setTimeout(() => setRemaining((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [remaining]);

  return (
    <div className="text-caption text-error mx-auto flex w-full max-w-3xl items-center gap-3 px-6 pb-2">
      <p>{error}</p>
      {canRetry && (
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          disabled={remaining > 0}
          onClick={onRetry}
        >
          {remaining > 0 ? `Try again in ${remaining}s` : "Try again"}
        </button>
      )}
    </div>
  );
}
