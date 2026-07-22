import { useState } from "react";
import { Upload } from "lucide-react";
import { useSources } from "../../api/hooks";
import { UploadForm } from "../uploads/UploadForm";

export interface MaterialsStepProps {
  value: string[];
  onChange: (sourceIds: string[]) => void;
  onNext: () => void;
}

export function MaterialsStep({ value, onChange, onNext }: MaterialsStepProps) {
  const { data: sources, isLoading } = useSources(undefined);
  const [showUploadForm, setShowUploadForm] = useState(false);

  function toggleSource(id: string) {
    if (value.includes(id)) {
      onChange(value.filter((sid) => sid !== id));
    } else {
      onChange([...value, id]);
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-6 py-8">
      <div className="flex flex-col gap-2">
        <h2 className="text-h2">Add learning materials</h2>
        <p className="text-caption text-base-content/60">
          Select sources to personalize your learning path (optional).
        </p>
      </div>

      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <p className="text-body font-medium">Your materials</p>
          <button
            onClick={() => setShowUploadForm(!showUploadForm)}
            className="btn btn-ghost btn-sm gap-1"
          >
            <Upload size={16} />
            Upload new
          </button>
        </div>

        {isLoading && <p className="text-caption text-base-content/50">Loading…</p>}

        {!isLoading && sources && sources.length === 0 && (
          <p className="text-caption text-base-content/50">
            No materials yet. Upload one to get started, or proceed without materials.
          </p>
        )}

        {!isLoading && sources && sources.length > 0 && (
          <div className="flex flex-col gap-1">
            {sources.map((source) => (
              <label
                key={source.id}
                className="text-body flex items-center gap-2 rounded-field border border-base-300 px-3 py-2 hover:bg-base-200 transition-colors"
              >
                <input
                  type="checkbox"
                  className="checkbox checkbox-sm"
                  checked={value.includes(source.id)}
                  onChange={() => toggleSource(source.id)}
                />
                <span className="truncate">{source.origin}</span>
              </label>
            ))}
          </div>
        )}

        {showUploadForm && (
          <div className="border-base-300 rounded-box border p-4 bg-base-100">
            <UploadForm subjects={[]} />
          </div>
        )}
      </div>

      <div className="flex justify-end gap-3">
        <button onClick={onNext} className="btn btn-primary">
          Next
        </button>
      </div>
    </div>
  );
}
