import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { AlertCircle, Loader, BookOpen } from "lucide-react";
import type { CurriculumProposal } from "../../api/onboarding";
import { useCommitSubject } from "../../api/onboarding";

export interface CommitStepProps {
  curriculum: CurriculumProposal;
  sourceIds: string[];
  onBack: () => void;
}

export function CommitStep({ curriculum, sourceIds, onBack }: CommitStepProps) {
  const navigate = useNavigate();
  const commitSubject = useCommitSubject();

  // When a successful subject is created, navigate to lessons
  useEffect(() => {
    if (commitSubject.data) {
      navigate(`/app/lessons?subject_id=${commitSubject.data.id}`);
    }
  }, [commitSubject.data, navigate]);

  // Derive error message from mutation error
  const errorMessage = (() => {
    if (!commitSubject.error) return null;
    const err = commitSubject.error as Error & { status?: number };
    if (err.status === 409) {
      return "A subject with this name already exists — go back and rename it.";
    }
    return "Something went wrong. Try again.";
  })();

  function handleCreateSubject() {
    const payload = {
      subject_name: curriculum.subject_name,
      subject_description: curriculum.subject_description || null,
      topics: curriculum.topics,
      source_ids: sourceIds.length ? sourceIds : null,
    };
    commitSubject.mutate(payload);
  }

  const totalKCs = curriculum.topics.reduce((sum, topic) => sum + topic.kcs.length, 0);

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-6 py-8">
      <div className="flex flex-col gap-2">
        <h2 className="text-h2">Create your subject</h2>
        <p className="text-caption text-base-content/60">
          Review the summary and create your personalized learning subject.
        </p>
      </div>

      {/* Summary Card */}
      <div className="card bg-base-100 border border-primary border-opacity-30 p-6">
        <div className="flex flex-col gap-4">
          <div className="flex items-start gap-3">
            <BookOpen size={20} className="text-primary shrink-0 mt-0.5" />
            <div className="flex flex-col gap-1">
              <h3 className="text-body font-semibold text-base-content">
                {curriculum.subject_name}
              </h3>
              {curriculum.subject_description && (
                <p className="text-caption text-base-content/70">
                  {curriculum.subject_description}
                </p>
              )}
            </div>
          </div>

          <div className="divider my-1" />

          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col gap-1">
              <p className="text-caption text-base-content/60">Topics</p>
              <p className="text-h3">{curriculum.topics.length}</p>
            </div>
            <div className="flex flex-col gap-1">
              <p className="text-caption text-base-content/60">Knowledge components</p>
              <p className="text-h3">{totalKCs}</p>
            </div>
          </div>
        </div>
      </div>

      {/* Error Message */}
      {errorMessage && (
        <div className="flex items-start gap-3 rounded-box border border-error bg-error/5 p-4">
          <AlertCircle size={16} className="text-error shrink-0 mt-0.5" />
          <div className="flex flex-col gap-2">
            <p className="text-caption text-error">{errorMessage}</p>
            <div className="flex gap-2">
              <button
                onClick={handleCreateSubject}
                disabled={commitSubject.isPending}
                className="btn btn-error btn-xs"
              >
                {commitSubject.isPending ? (
                  <>
                    <Loader size={12} className="animate-spin" />
                    Retrying…
                  </>
                ) : (
                  "Retry"
                )}
              </button>
              <button onClick={onBack} className="btn btn-ghost btn-xs">
                Back
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Action Buttons */}
      <div className="flex justify-between gap-3">
        <button onClick={onBack} disabled={commitSubject.isPending} className="btn btn-ghost">
          Back
        </button>
        <button
          onClick={handleCreateSubject}
          disabled={commitSubject.isPending || !!errorMessage}
          className="btn btn-primary gap-1"
        >
          {commitSubject.isPending ? (
            <>
              <Loader size={16} className="animate-spin" />
              Creating…
            </>
          ) : (
            "Create subject"
          )}
        </button>
      </div>
    </div>
  );
}
