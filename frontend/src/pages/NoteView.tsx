import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { ArrowLeft, History, Loader2, Pencil } from "lucide-react";
import { useEditNote, useNote, useRefreshNote, useSetFormat, type NoteFormat } from "../api/notes";
import { HistoryDrawer } from "../components/notes/HistoryDrawer";

const FORMAT_LABELS: Record<NoteFormat, string> = {
  outline: "Outline",
  narrative: "Narrative",
  mnemonic: "Mnemonics",
  worked_examples: "Worked examples",
};

// No @tailwindcss/typography plugin in this app — style the rendered markdown with our own
// type-scale classes rather than an unstyled (or unavailable) `prose` class.
const MARKDOWN_CLASSES =
  "flex flex-col gap-3 [&_h1]:text-h2 [&_h2]:text-h3 [&_h3]:text-body [&_h3]:font-semibold " +
  "[&_p]:text-body [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:list-decimal [&_ol]:pl-6 " +
  "[&_li]:text-body [&_strong]:font-semibold [&_code]:bg-base-200 [&_code]:rounded-field [&_code]:px-1 " +
  "[&_blockquote]:border-primary/30 [&_blockquote]:text-base-content/70 [&_blockquote]:border-l-2 [&_blockquote]:pl-4";

export function NoteView() {
  const { topicId } = useParams();
  const id = topicId!;
  const { data: note } = useNote(id);
  const refresh = useRefreshNote(id);
  const edit = useEditNote(id);
  const setFormat = useSetFormat(id);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const refreshedRef = useRef(false);

  // Catch-up on read: one auto-refresh when the note arrives stale (ref-guarded — the
  // invalidation after refresh refetches the note, which must not loop).
  useEffect(() => {
    if (note?.stale && !refreshedRef.current && !refresh.isPending) {
      refreshedRef.current = true;
      refresh.mutate();
    }
  }, [note?.stale, refresh]);

  if (!note) {
    return <p className="text-caption text-base-content/50">Loading…</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <Link to="/app/notes" className="btn btn-ghost btn-sm">
          <ArrowLeft size={16} /> All notes
        </Link>
        <div className="flex items-center gap-2">
          <select
            className="select select-sm"
            value={note.format ?? "auto"}
            onChange={(e) => {
              const v = e.target.value;
              setFormat.mutate(v === "auto" ? null : (v as NoteFormat));
            }}
          >
            <option value="auto">Auto ({FORMAT_LABELS[note.effective_format]})</option>
            {(Object.keys(FORMAT_LABELS) as NoteFormat[]).map((f) => (
              <option key={f} value={f}>
                {FORMAT_LABELS[f]}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setHistoryOpen(true)}
          >
            <History size={16} /> History
          </button>
          {note.content_md !== null && !editing && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setDraft(note.content_md ?? "");
                setEditing(true);
              }}
            >
              <Pencil size={16} /> Edit
            </button>
          )}
        </div>
      </div>

      {(refresh.isPending || setFormat.isPending) && (
        <div className="alert alert-info flex items-center gap-2">
          <Loader2 size={16} className="animate-spin" /> Updating your notes…
        </div>
      )}
      {edit.isError && (
        <div className="alert alert-error">
          {edit.error instanceof Error
            ? edit.error.message
            : "Edit could not be absorbed. Your note is unchanged — please try again."}
        </div>
      )}

      {editing ? (
        <div className="flex flex-col gap-3">
          <textarea
            className="textarea textarea-bordered min-h-96 w-full font-mono"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="flex gap-2">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={edit.isPending || draft.trim().length === 0}
              onClick={() => edit.mutate(draft, { onSuccess: () => setEditing(false) })}
            >
              {edit.isPending ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : note.content_md !== null ? (
        <article className={MARKDOWN_CLASSES}>
          <ReactMarkdown>{note.content_md}</ReactMarkdown>
        </article>
      ) : (
        !refresh.isPending && (
          <p className="text-base-content/60">
            No notes for this topic yet — they'll appear automatically once you've studied it.
          </p>
        )
      )}

      <HistoryDrawer topicId={id} open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </div>
  );
}
