import { useState } from "react";

const CONFIRM =
  "This source is already in your library. Re-processing it replaces its passages; older " +
  "replies will show their citations as an earlier version.";

/** The API's reason for refusing, whichever shape it came in: `{code, message}` for the two
 * 409s, a plain string for the 403 and 404. */
function refusal(error: unknown): string | null {
  if (!error) return null;
  const detail = (error as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof (detail as { message?: unknown }).message === "string") {
    return (detail as { message: string }).message;
  }
  return "That didn't work. Try again in a moment.";
}

/** Retry for a failed source; Re-process, confirmed first, for a finished one (S29). A
 * finished source's passages may be cited by replies the learner already has, so replacing
 * them is their decision, made with the consequence in front of them. */
export function SourceActions({
  status,
  kind,
  onRetry,
  pending,
  error,
}: {
  status: string;
  kind?: string;
  onRetry: (confirm: boolean) => void;
  pending?: boolean;
  error?: unknown;
}) {
  const [asking, setAsking] = useState(false);
  const reason = refusal(error);
  // A web source cannot be ingested in v0, so offering to retry one only invites a refusal.
  if (kind === "url") return null;
  const failure = reason && <p className="text-caption text-error max-w-xs text-right">{reason}</p>;
  if (status === "failed") {
    return (
      <div className="flex flex-col items-end gap-1">
        <button className="btn btn-ghost btn-xs" disabled={pending} onClick={() => onRetry(false)}>
          Retry
        </button>
        {failure}
      </div>
    );
  }
  if (status !== "done") return null;
  if (!asking) {
    return (
      <div className="flex flex-col items-end gap-1">
        <button className="btn btn-ghost btn-xs" disabled={pending} onClick={() => setAsking(true)}>
          Re-process
        </button>
        {failure}
      </div>
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
