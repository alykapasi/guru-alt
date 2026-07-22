import { Trash2, RotateCcw, Loader } from "lucide-react";
import type { CurriculumProposal } from "../../api/onboarding";
import { useGenerateCurriculum } from "../../api/onboarding";

export interface ReviewStepProps {
  curriculum: CurriculumProposal;
  goal: string;
  sourceIds: string[];
  onChange: (next: CurriculumProposal) => void;
  onNext: () => void;
  onBack: () => void;
}

export function ReviewStep({
  curriculum,
  goal,
  sourceIds,
  onChange,
  onNext,
  onBack,
}: ReviewStepProps) {
  const generateCurriculum = useGenerateCurriculum();

  function handleSubjectNameChange(value: string) {
    onChange({
      ...curriculum,
      subject_name: value,
    });
  }

  function handleSubjectDescriptionChange(value: string) {
    onChange({
      ...curriculum,
      subject_description: value,
    });
  }

  function handleRemoveTopic(topicIndex: number) {
    const newTopics = curriculum.topics.filter((_, i) => i !== topicIndex);
    onChange({
      ...curriculum,
      topics: newTopics,
    });
  }

  function handleEditTopic(topicIndex: number, key: "name" | "description", value: string) {
    const newTopics = curriculum.topics.map((topic, i) =>
      i === topicIndex ? { ...topic, [key]: value } : topic,
    );
    onChange({
      ...curriculum,
      topics: newTopics,
    });
  }

  function handleRemoveKC(topicIndex: number, kcIndex: number) {
    const newTopics = curriculum.topics.map((topic, i) => {
      if (i === topicIndex) {
        return {
          ...topic,
          kcs: topic.kcs.filter((_, j) => j !== kcIndex),
        };
      }
      return topic;
    });
    onChange({
      ...curriculum,
      topics: newTopics,
    });
  }

  function handleEditKC(
    topicIndex: number,
    kcIndex: number,
    key: "name" | "description",
    value: string,
  ) {
    const newTopics = curriculum.topics.map((topic, i) => {
      if (i === topicIndex) {
        return {
          ...topic,
          kcs: topic.kcs.map((kc, j) => (j === kcIndex ? { ...kc, [key]: value } : kc)),
        };
      }
      return topic;
    });
    onChange({
      ...curriculum,
      topics: newTopics,
    });
  }

  function handleRegenerate() {
    generateCurriculum.mutate(
      { goal, sourceIds: sourceIds.length ? sourceIds : null },
      {
        onSuccess: (fresh) => {
          onChange(fresh);
        },
      },
    );
  }

  const isNextDisabled = !curriculum.subject_name.trim() || curriculum.topics.length === 0;

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-6 py-8">
      <div className="flex flex-col gap-2">
        <h2 className="text-h2">Review your curriculum</h2>
        <p className="text-caption text-base-content/60">
          Edit the subject, topics, and knowledge components. You can rename, remove, or regenerate.
        </p>
      </div>

      <div className="flex flex-col gap-6">
        {/* Subject Card */}
        <div className="card bg-base-100 border border-base-300 p-4">
          <div className="flex flex-col gap-3">
            <p className="text-body font-medium">Subject</p>
            <input
              type="text"
              value={curriculum.subject_name}
              onChange={(e) => handleSubjectNameChange(e.target.value)}
              placeholder="Subject name"
              className="input input-bordered text-body"
            />
            <textarea
              value={curriculum.subject_description}
              onChange={(e) => handleSubjectDescriptionChange(e.target.value)}
              placeholder="Subject description (optional)"
              className="textarea textarea-bordered text-body resize-none"
              rows={2}
            />
          </div>
        </div>

        {/* Topics Card */}
        <div className="card bg-base-100 border border-base-300 p-4">
          <div className="flex flex-col gap-3">
            <p className="text-body font-medium">Topics ({curriculum.topics.length})</p>

            {curriculum.topics.length === 0 && (
              <p className="text-caption text-base-content/50">No topics yet.</p>
            )}

            {curriculum.topics.map((topic, topicIndex) => (
              <div
                key={topicIndex}
                className="flex flex-col gap-3 rounded-field border border-base-300 bg-base-200/30 p-3"
              >
                {/* Topic Header */}
                <div className="flex items-center justify-between gap-2">
                  <input
                    type="text"
                    value={topic.name}
                    onChange={(e) => handleEditTopic(topicIndex, "name", e.target.value)}
                    placeholder="Topic name"
                    className="input input-sm input-bordered text-body flex-1"
                  />
                  <button
                    onClick={() => handleRemoveTopic(topicIndex)}
                    className="btn btn-ghost btn-sm"
                    title="Remove topic"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>

                {/* Topic Description */}
                <textarea
                  value={topic.description}
                  onChange={(e) => handleEditTopic(topicIndex, "description", e.target.value)}
                  placeholder="Topic description"
                  className="textarea textarea-sm textarea-bordered text-body resize-none"
                  rows={2}
                />

                {/* Knowledge Components */}
                <div className="flex flex-col gap-2">
                  <p className="text-caption text-base-content/70">
                    Knowledge components ({topic.kcs.length})
                  </p>

                  {topic.kcs.length === 0 && (
                    <p className="text-caption text-base-content/50">No KCs yet.</p>
                  )}

                  {topic.kcs.map((kc, kcIndex) => (
                    <div
                      key={kcIndex}
                      className="flex flex-col gap-2 rounded-field border border-base-300/50 bg-base-100 p-2"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <input
                          type="text"
                          value={kc.name}
                          onChange={(e) =>
                            handleEditKC(topicIndex, kcIndex, "name", e.target.value)
                          }
                          placeholder="KC name"
                          className="input input-xs input-bordered text-body flex-1"
                        />
                        <button
                          onClick={() => handleRemoveKC(topicIndex, kcIndex)}
                          className="btn btn-ghost btn-xs"
                          title="Remove KC"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                      <textarea
                        value={kc.description}
                        onChange={(e) =>
                          handleEditKC(topicIndex, kcIndex, "description", e.target.value)
                        }
                        placeholder="KC description"
                        className="textarea textarea-xs textarea-bordered text-body resize-none"
                        rows={1}
                      />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Controls */}
      <div className="flex flex-col gap-3">
        <button
          onClick={handleRegenerate}
          disabled={generateCurriculum.isPending}
          className="btn btn-outline gap-1"
        >
          {generateCurriculum.isPending ? (
            <>
              <Loader size={16} className="animate-spin" />
              Regenerating…
            </>
          ) : (
            <>
              <RotateCcw size={16} />
              Try again
            </>
          )}
        </button>

        <div className="flex justify-between gap-3">
          <button onClick={onBack} className="btn btn-ghost">
            Back
          </button>
          <button onClick={onNext} disabled={isNextDisabled} className="btn btn-primary">
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
