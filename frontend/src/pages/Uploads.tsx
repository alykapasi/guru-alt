import { useState } from "react";
import { useAllSources, useSubjects } from "../api/hooks";
import { SubjectPicker } from "../components/SubjectPicker";
import { SourceList } from "../components/uploads/SourceList";
import { UploadForm } from "../components/uploads/UploadForm";

export function Uploads() {
  const { data: subjects } = useSubjects();
  const [filterSubjectId, setFilterSubjectId] = useState<string | null>(null);
  const { data: sources, isLoading } = useAllSources(filterSubjectId ?? undefined);

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-h1">Uploads</h1>
      <UploadForm subjects={subjects ?? []} />
      <div className="border-base-300 flex flex-col gap-4 rounded-box border p-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h2 className="text-h2">Your materials</h2>
          {subjects && subjects.length > 0 && (
            <SubjectPicker
              subjects={subjects}
              selectedId={filterSubjectId}
              onSelect={setFilterSubjectId}
              allLabel="All"
            />
          )}
        </div>
        <SourceList sources={sources ?? []} isLoading={isLoading} />
      </div>
    </div>
  );
}
