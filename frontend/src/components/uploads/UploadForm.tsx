import { useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { FileUp } from "lucide-react";
import { useUploadSource } from "../../api/hooks";
import type { components } from "../../api/schema";

type Subject = components["schemas"]["SubjectRead"];

/** Upload a file for ingestion, optionally tagged to a subject at
 * upload time (untagged = general library material, per Source.subject_id being nullable). */
export function UploadForm({ subjects }: { subjects: Subject[] }) {
  const upload = useUploadSource();
  const [subjectId, setSubjectId] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    upload.mutate({ file, subjectId: subjectId || undefined });
  }

  return (
    <div className="border-base-300 flex flex-col gap-4 rounded-box border p-6">
      <div className="flex items-center gap-2">
        <label className="text-caption text-base-content/60" htmlFor="upload-subject">
          Subject
        </label>
        <select
          id="upload-subject"
          value={subjectId}
          onChange={(e) => setSubjectId(e.target.value)}
          className="select select-sm"
        >
          <option value="">General</option>
          {subjects.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>
      <p className="text-caption text-base-content/50 -mt-2">
        Without a subject, a file is only used by subjects that opt in to untagged materials.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={upload.isPending}
          className="btn btn-outline btn-sm"
        >
          <FileUp size={14} />
          Upload a file
        </button>
        <input ref={fileInputRef} type="file" onChange={handleFileChange} className="hidden" />
      </div>
      {upload.isPending && <p className="text-caption text-base-content/50">Uploading…</p>}
      {upload.isError && <p className="text-caption text-error">Upload failed — try again.</p>}
    </div>
  );
}
