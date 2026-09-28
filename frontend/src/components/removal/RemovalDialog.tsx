import { useState } from "react";
import { useRemovalImpact, useRemove } from "../../api/hooks";
import { FORGET_COPY, KEPT_COPY, describeCounts } from "./removalCopy";

type Kind = "source" | "conversation";

function refusal(error: unknown): string | null {
  const detail = (error as { detail?: { message?: string } } | null)?.detail;
  return detail?.message ?? (error ? "That didn't work. Try again." : null);
}

/** Delete now, after saying what stays — and optionally forget what was learned (S61, V11). */
export function RemovalDialog({
  kind,
  id,
  name,
  open,
  onClose,
  onDeleted,
}: {
  kind: Kind;
  id: string;
  name: string;
  open: boolean;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const [forget, setForget] = useState(false);
  const impact = useRemovalImpact(kind, id, open);
  const remove = useRemove(kind);
  const forgettableCounts = impact.data?.forgettable ?? {};
  // Once forgetting is ticked, what it removes is no longer "kept" — say each thing once.
  const kept = describeCounts(
    Object.fromEntries(
      Object.entries(impact.data?.kept ?? {}).filter(
        ([key]) => !(forget && key in forgettableCounts),
      ),
    ),
    KEPT_COPY,
  );
  const forgettable = describeCounts(forgettableCounts, FORGET_COPY);
  const message = refusal(remove.error);

  return (
    <dialog className="modal" open={open}>
      <div className="modal-box flex flex-col gap-3">
        <h3 className="text-h3">Delete {name}?</h3>
        {impact.isLoading ? (
          <p className="text-caption text-base-content/50">Checking what this affects…</p>
        ) : (
          <>
            {kept.length > 0 && (
              <div>
                <p className="text-body">These stay:</p>
                <ul className="text-caption list-disc pl-5">
                  {kept.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </div>
            )}
            {(impact.data?.notes ?? []).map((note) => (
              <p key={note} className="text-caption text-base-content/70">
                {note}
              </p>
            ))}
            {forgettable.length > 0 && (
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="checkbox checkbox-sm"
                  checked={forget}
                  onChange={(e) => setForget(e.target.checked)}
                />
                <span className="text-body">
                  Also forget what was learned from this
                  <span className="text-caption text-base-content/60 block">
                    Removes {forgettable.join(", ")}.
                  </span>
                </span>
              </label>
            )}
          </>
        )}
        {message && <p className="text-caption text-error">{message}</p>}
        <div className="modal-action">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-error btn-sm"
            disabled={remove.isPending || impact.isLoading}
            onClick={() =>
              remove.mutate({ id, forget }, { onSuccess: () => (onDeleted ?? onClose)() })
            }
          >
            Delete
          </button>
        </div>
      </div>
    </dialog>
  );
}
