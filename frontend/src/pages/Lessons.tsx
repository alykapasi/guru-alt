import { useState } from "react";
import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import { useSubjects, useUpdateSourceSettings } from "../api/hooks";
import type { components } from "../api/schema";
import { PublishPanel } from "../components/PublishPanel";
import { LessonPlanPanel } from "../components/lessons/LessonPlanPanel";
import { CurriculumIssuesPanel } from "../components/lessons/CurriculumIssuesPanel";
import { ConnectionsPanel } from "../components/lessons/ConnectionsPanel";
import { SourceSettingsPanel } from "../components/lessons/SourceSettingsPanel";
import { EmptyPage } from "../components/EmptyPage";
import { SubjectPicker } from "../components/SubjectPicker";

type Subject = components["schemas"]["SubjectRead"];

/** The owner's source switches for one subject (S26), wired to the API. */
function SubjectSources({ subject }: { subject: Subject }) {
  const update = useUpdateSourceSettings(subject.id);
  return (
    <SourceSettingsPanel
      includeUntagged={subject.include_untagged_sources ?? false}
      sourcesOnly={subject.sources_only ?? false}
      onChange={(patch) => update.mutate(patch)}
      disabled={update.isPending}
    />
  );
}

export function Lessons() {
  const { data: subjects, isLoading } = useSubjects();
  const [pickedId, setPickedId] = useState<string | null>(null);
  const selectedId = pickedId ?? subjects?.[0]?.id ?? null;
  const selected = subjects?.find((s) => s.id === selectedId) ?? null;

  if (isLoading) {
    return <p className="text-caption text-base-content/70">Loading subjects…</p>;
  }

  if (!subjects || subjects.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <EmptyPage
          title="Lessons"
          description="No subjects yet — add one to get a personalized lesson plan and guided practice."
        />
        <div>
          <Link to="/app/subjects/new" className="btn btn-primary">
            Create your first subject
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-h1">Lessons</h1>
        <Link to="/app/subjects/new" className="btn btn-primary btn-sm">
          <Plus size={16} aria-hidden />
          New subject
        </Link>
      </div>
      <SubjectPicker subjects={subjects} selectedId={selectedId} onSelect={setPickedId} />
      {selectedId && <LessonPlanPanel key={selectedId} subjectId={selectedId} />}
      {/* Every selected subject, not only owned ones — a curated subject can be linked too. */}
      {selectedId && <ConnectionsPanel key={`links-${selectedId}`} subjectId={selectedId} />}
      {/* Only on a subject that is theirs. A curated one is the shared library, which is
          nobody's to publish — offering the action there and letting the API refuse would be
          showing a door that answers 404. */}
      {selected && selected.owner_learner_id !== null && (
        <>
          <CurriculumIssuesPanel key={`issues-${selected.id}`} subjectId={selected.id} />
          {/* Set once and rarely revisited, so folded under the plan rather than competing with
              it. */}
          <details className="border-base-300 rounded-box border px-4 py-2">
            <summary className="text-body cursor-pointer">Sources and sharing</summary>
            <div className="flex flex-col gap-6 pt-3">
              <SubjectSources key={`sources-${selected.id}`} subject={selected} />
              <PublishPanel
                key={selected.id}
                subjectId={selected.id}
                sourceDerived={selected.private_source_derived}
              />
            </div>
          </details>
        </>
      )}
    </div>
  );
}
