import { useState } from "react";
import { Link } from "react-router-dom";
import { NotebookText } from "lucide-react";
import { useSubjects } from "../api/hooks";
import { PublishPanel } from "../components/PublishPanel";
import { LessonPlanPanel } from "../components/lessons/LessonPlanPanel";
import { PlaceholderPage } from "../components/PlaceholderPage";
import { SubjectPicker } from "../components/SubjectPicker";

export function Lessons() {
  const { data: subjects, isLoading } = useSubjects();
  const [pickedId, setPickedId] = useState<string | null>(null);
  const selectedId = pickedId ?? subjects?.[0]?.id ?? null;
  const selected = subjects?.find((s) => s.id === selectedId) ?? null;

  if (isLoading) {
    return <p className="text-caption text-base-content/50">Loading subjects…</p>;
  }

  if (!subjects || subjects.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <PlaceholderPage
          icon={NotebookText}
          title="Lessons"
          description="No subjects yet — add one to get a personalized lesson plan and guided practice."
        />
        <div className="max-w-2xl">
          <Link to="/app/subjects/new" className="btn btn-primary">
            Create your first subject
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-h1">Lessons</h1>
      <SubjectPicker subjects={subjects} selectedId={selectedId} onSelect={setPickedId} />
      {selectedId && <LessonPlanPanel key={selectedId} subjectId={selectedId} />}
      {/* Only on a subject that is theirs. A curated one is the shared library, which is
          nobody's to publish — offering the action there and letting the API refuse would be
          showing a door that answers 404. */}
      {selected && selected.owner_learner_id !== null && (
        <PublishPanel
          key={selected.id}
          subjectId={selected.id}
          sourceDerived={selected.private_source_derived}
        />
      )}
    </div>
  );
}
