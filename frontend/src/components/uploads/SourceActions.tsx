import { useState } from "react";

const CONFIRM =
  "This source is already in your library. Re-processing it replaces its passages; older " +
  "replies will show their citations as an earlier version.";

/** Retry for a failed source; Re-process, confirmed first, for a finished one (S29). A
 * finished source's passages may be cited by replies the learner already has, so replacing
 * them is their decision, made with the consequence in front of them. */
export function SourceActions({
  status,
  onRetry,
  pending,
}: {
  status: string;
  onRetry: (confirm: boolean) => void;
  pending?: boolean;
}) {
  const [asking, setAsking] = useState(false);
  if (status === "failed") {
    return (
      <button className="btn btn-ghost btn-xs" disabled={pending} onClick={() => onRetry(false)}>
        Retry
      </button>
    );
  }
  if (status !== "done") return null;
  if (!asking) {
    return (
      <button className="btn btn-ghost btn-xs" disabled={pending} onClick={() => setAsking(true)}>
        Re-process
      </button>
    );
  }
  return (
    <div className="flex flex-col items-end gap-1">
      <p className="text-caption text-base-content/70 max-w-xs text-right">{CONFIRM}</p>
      <div className="flex gap-1">
        <button className="btn btn-ghost btn-xs" onClick={() => setAsking(false)}>
          Cancel
        </button>
        <button
          className="btn btn-warning btn-xs"
          disabled={pending}
          onClick={() => {
            setAsking(false);
            onRetry(true);
          }}
        >
          Replace passages
        </button>
      </div>
    </div>
  );
}
