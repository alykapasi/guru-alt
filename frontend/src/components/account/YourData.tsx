import { API_BASE_URL } from "../../api/client";
import { useExportFiles } from "../../api/hooks";

/** The learner's data to take with them (S61): the JSON export, and each uploaded file as its
 * own download — a path inside the JSON is not something a person can click. */
export function YourData() {
  const { data: files } = useExportFiles();
  return (
    <div className="flex flex-col gap-2">
      <a className="link text-body" href={`${API_BASE_URL}/api/v1/me/export`}>
        Download your data (JSON)
      </a>
      {files && files.length > 0 && (
        <ul className="text-body flex flex-col gap-1">
          {files.map((file) => (
            <li key={file.id}>
              <a className="link" href={`${API_BASE_URL}${file.file_path}`}>
                {file.origin}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
