import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, History, Loader2, Pencil } from "lucide-react";
import { useEditNote, useNote, useRefreshNote, useSetFormat, type NoteFormat } from "../api/notes";
import { HistoryDrawer } from "../components/notes/HistoryDrawer";
import { RichText } from "../components/content/RichText";

const FORMAT_LABELS: Record<NoteFormat, string> = {
  outline: "Outline",
  narrative: "Narrative",
  mnemonic: "Mnemonics",
  worked_examples: "Worked examples",
};

// No @tailwindcss/typography plugin in this app — style the rendered markdown with our own
// type-scale classes rather than an unstyled (or unavailable) `prose` class.
export function NoteView() {
  const { topicId } = useParams();
  const id = topicId!;
  const { data: note } = useNote(id);
  const refresh = useRefreshNote(id);
  const edit = useEditNote(id);
  const setFormat = useSetFormat(id);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  // The revision this draft was written against — sent on save so a note that changed
  // underneath (a background refresh, another tab) is a conflict rather than a silent
  // overwrite. The editor stays open on that error, so the text is never lost.
  const [draftBase, setDraftBase] = useState<number | null>(null);
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
                setDraftBase(note.revision_ordinal);
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
              onClick={() =>
                edit.mutate(
                  { contentMd: draft, expectedRevisionOrdinal: draftBase },
                  { onSuccess: () => setEditing(false) },
                )
              }
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
        <article>
          <RichText content={note.content_md} />
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
