import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { BookOpen, Check } from "lucide-react";
import { useCreateConversation, useSources, useSubjects } from "../../api/hooks";

/** Picks a conversation's retrieval scope before creating it — a subject (hard content
 * boundary, see MASTERPLAN §7 "Content boundary") or "General" (no library grounding), then
 * optionally narrows to specific sources within that subject's materials. */
export function NewChatModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const navigate = useNavigate();
  const { data: subjects } = useSubjects();
  const [subjectId, setSubjectId] = useState<string | null>(null);
  const { data: sources } = useSources(subjectId ?? undefined);
  const [sourceIds, setSourceIds] = useState<Set<string>>(new Set());
  const createConversation = useCreateConversation();

  useEffect(() => {
    if (open) dialogRef.current?.showModal();
    else dialogRef.current?.close();
  }, [open]);

  function handleClose() {
    setSubjectId(null);
    setSourceIds(new Set());
    onClose();
  }

  function toggleSource(id: string) {
    setSourceIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function start() {
    createConversation.mutate(
      {
        subject_id: subjectId ?? undefined,
        source_ids: subjectId ? Array.from(sourceIds) : undefined,
      },
      { onSuccess: (created) => navigate(`/app/chat/${created.id}`) },
    );
    handleClose();
  }

  return (
    <dialog ref={dialogRef} className="modal" onClose={handleClose}>
      <div className="modal-box flex flex-col gap-5">
        <h2 className="text-h2">New chat</h2>

        <div className="flex flex-col gap-2">
          <p className="text-caption text-base-content/60">What can this chat draw on?</p>
          <button
            onClick={() => setSubjectId(null)}
            className={`text-body flex items-center justify-between rounded-field border px-3 py-2 text-left transition-colors ${
              subjectId === null
                ? "border-primary bg-primary/10"
                : "border-base-300 hover:bg-base-200"
            }`}
          >
            General — no library grounding
            {subjectId === null && <Check size={16} className="text-primary" />}
          </button>
          {subjects?.map((s) => (
            <button
              key={s.id}
              onClick={() => setSubjectId(s.id)}
              className={`text-body flex items-center justify-between rounded-field border px-3 py-2 text-left transition-colors ${
                subjectId === s.id
                  ? "border-primary bg-primary/10"
                  : "border-base-300 hover:bg-base-200"
              }`}
            >
              <span className="flex items-center gap-2">
                <BookOpen size={16} className="text-base-content/50" />
                {s.name}
              </span>
              {subjectId === s.id && <Check size={16} className="text-primary" />}
            </button>
          ))}
        </div>

        {subjectId && (
          <div className="flex flex-col gap-2">
            <p className="text-caption text-base-content/60">
              Narrow to specific sources (optional — leave empty for all materials in this subject)
            </p>
            {sources?.length === 0 && (
              <p className="text-caption text-base-content/40">No sources in this subject yet.</p>
            )}
            {sources?.map((s) => (
              <label
                key={s.id}
                className="text-body flex items-center gap-2 rounded-field px-3 py-2 hover:bg-base-200"
              >
                <input
                  type="checkbox"
                  className="checkbox checkbox-sm"
                  checked={sourceIds.has(s.id)}
                  onChange={() => toggleSource(s.id)}
                />
                <span className="truncate">{s.origin}</span>
              </label>
            ))}
          </div>
        )}

        <div className="modal-action">
          <button onClick={handleClose} className="btn btn-ghost">
            Cancel
          </button>
          <button onClick={start} className="btn btn-primary">
            Start chat
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button onClick={handleClose}>close</button>
      </form>
    </dialog>
  );
}
