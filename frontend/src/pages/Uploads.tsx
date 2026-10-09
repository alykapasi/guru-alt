import { useState } from "react";
import { useAllSources, useArchivedSources, useSubjects } from "../api/hooks";
import { SubjectPicker } from "../components/SubjectPicker";
import { SourceList } from "../components/uploads/SourceList";
import { UploadForm } from "../components/uploads/UploadForm";
import { LoadMore } from "../components/LoadMore";

export function Uploads() {
  const { data: subjects } = useSubjects();
  const [filterSubjectId, setFilterSubjectId] = useState<string | null>(null);
  const {
    data: sources,
    isLoading,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useAllSources(filterSubjectId ?? undefined);
  const archivedQuery = useArchivedSources();
  const archived = archivedQuery.data;

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
        <LoadMore
          hasNextPage={hasNextPage}
          isFetchingNextPage={isFetchingNextPage}
          fetchNextPage={fetchNextPage}
          className="btn btn-ghost btn-sm self-center"
        />
        {archived && archived.length > 0 && (
          <details className="border-base-300 border-t pt-3">
            <summary className="text-caption text-base-content/60 cursor-pointer">
              Archived ({archived.length}
              {archivedQuery.hasNextPage ? "+" : ""})
            </summary>
            <div className="pt-2">
              <SourceList sources={archived} isLoading={false} archived />
              <LoadMore
                hasNextPage={archivedQuery.hasNextPage}
                isFetchingNextPage={archivedQuery.isFetchingNextPage}
                fetchNextPage={archivedQuery.fetchNextPage}
              />
            </div>
          </details>
        )}
      </div>
    </div>
  );
}
