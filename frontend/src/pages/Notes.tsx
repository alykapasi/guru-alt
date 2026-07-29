import { useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen } from "lucide-react";
import { useSubjects } from "../api/hooks";
import { useNotesIndex } from "../api/notes";
import { SubjectPicker } from "../components/SubjectPicker";
import { PlaceholderPage } from "../components/PlaceholderPage";

export function Notes() {
  const { data: subjects, isLoading } = useSubjects();
  const [pickedId, setPickedId] = useState<string | null>(null);
  const selectedId = pickedId ?? subjects?.[0]?.id ?? null;
  const { data: entries } = useNotesIndex(selectedId);

  if (isLoading) {
    return <p className="text-caption text-base-content/50">Loading subjects…</p>;
  }

  if (!subjects || subjects.length === 0) {
    return (
      <PlaceholderPage
        icon={BookOpen}
        title="Notes"
        description="Your notes grow automatically as you study — add a subject to begin."
      />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-h1">Notes</h1>
      <SubjectPicker subjects={subjects} selectedId={selectedId} onSelect={setPickedId} />
      <ul className="flex flex-col gap-2">
        {entries?.map((entry) => (
          <li key={entry.topic_id}>
            <Link
              to={`/app/notes/${entry.topic_id}`}
              className="border-base-300 hover:bg-base-200 flex items-center justify-between rounded-field border px-4 py-3 transition-colors"
            >
              <span className="flex items-center gap-3">
                <BookOpen size={16} className="text-base-content/50" />
                {entry.topic_name}
              </span>
              <span className="flex items-center gap-2">
                {entry.stale && <span className="badge badge-warning badge-sm">new material</span>}
                {!entry.has_note && !entry.stale && (
                  <span className="text-caption text-base-content/40">no notes yet</span>
                )}
              </span>
            </Link>
          </li>
        ))}
        {entries?.length === 0 && (
          <p className="text-caption text-base-content/50">No topics in this subject yet.</p>
        )}
      </ul>
    </div>
  );
}
