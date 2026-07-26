import { useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { FileUp, Link2 } from "lucide-react";
import { useLinkSource, useUploadSource } from "../../api/hooks";
import type { components } from "../../api/schema";

type Subject = components["schemas"]["SubjectRead"];

/** Upload a file or link a web page for ingestion, optionally tagged to a subject at
 * upload time (untagged = general library material, per Source.subject_id being nullable). */
export function UploadForm({ subjects }: { subjects: Subject[] }) {
  const upload = useUploadSource();
  const link = useLinkSource();
  const [subjectId, setSubjectId] = useState("");
  const [url, setUrl] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    upload.mutate({ file, subjectId: subjectId || undefined });
  }

  function submitLink() {
    const trimmed = url.trim();
    if (!trimmed) return;
    link.mutate(
      { url: trimmed, subjectId: subjectId || undefined },
      { onSuccess: () => setUrl("") },
    );
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
        <div className="flex items-center gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submitLink();
              }
            }}
            placeholder="https://…"
            className="input input-sm"
          />
          <button
            onClick={submitLink}
            disabled={!url.trim() || link.isPending}
            className="btn btn-outline btn-sm"
          >
            <Link2 size={14} />
            Add link
          </button>
        </div>
      </div>
      {upload.isPending && <p className="text-caption text-base-content/50">Uploading…</p>}
      {upload.isError && <p className="text-caption text-error">Upload failed — try again.</p>}
      {link.isError && <p className="text-caption text-error">Couldn't add that link.</p>}
    </div>
  );
}
