import { useState } from "react";
import { History, RotateCcw, X } from "lucide-react";
import {
  useRestoreRevision,
  useRevisions,
  useRevisionSource,
  type NoteRevisionRead,
} from "../../api/notes";

const CAUSE_LABELS: Record<NoteRevisionRead["cause"], string> = {
  distill: "Auto-updated from study",
  learner_edit: "Your edit",
  restore: "Restored",
};

interface Props {
  topicId: string;
  open: boolean;
  onClose: () => void;
}

/** Slide-over listing every revision; viewing shows the mechanical source, restore is a new
 * revision (history is never rewritten server-side). */
export function HistoryDrawer({ topicId, open, onClose }: Props) {
  const [viewing, setViewing] = useState<number | null>(null);
  const { data: revisions } = useRevisions(topicId, open);
  const { data: source } = useRevisionSource(topicId, viewing);
  const restore = useRestoreRevision(topicId);

  if (!open) return null;

  return (
    <aside className="border-base-300 bg-base-100 fixed inset-y-0 right-0 z-20 flex w-96 flex-col gap-4 overflow-y-auto border-l p-6 shadow-lg">
      <div className="flex items-center justify-between">
        <h2 className="text-h3 flex items-center gap-2">
          <History size={18} /> History
        </h2>
        <button type="button" className="btn btn-ghost btn-sm btn-square" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <ul className="flex flex-col gap-2">
        {revisions
          ?.slice()
          .reverse()
          .map((rev) => (
            <li key={rev.ordinal} className="border-base-300 rounded-field border p-3">
              <div className="flex items-center justify-between">
                <span className="text-caption">
                  #{rev.ordinal} — {CAUSE_LABELS[rev.cause]}
                </span>
                <span className="flex gap-1">
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs"
                    onClick={() => setViewing(viewing === rev.ordinal ? null : rev.ordinal)}
                  >
                    {viewing === rev.ordinal ? "Hide" : "View"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs"
                    disabled={restore.isPending}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Restore revision #${rev.ordinal}? Your current note stays in history.`,
                        )
                      ) {
                        restore.mutate(rev.ordinal, { onSuccess: onClose });
                      }
                    }}
                  >
                    <RotateCcw size={12} /> Restore
                  </button>
                </span>
              </div>
              <p className="text-caption text-base-content/50">
                {new Date(rev.created_at).toLocaleString()}
              </p>
              {viewing === rev.ordinal && source && (
                <pre className="bg-base-200 text-caption mt-2 max-h-64 overflow-auto rounded-field p-2 whitespace-pre-wrap">
                  {source.content_md}
                </pre>
              )}
            </li>
          ))}
        {revisions?.length === 0 && (
          <p className="text-caption text-base-content/50">No revisions yet.</p>
        )}
      </ul>
    </aside>
  );
}
