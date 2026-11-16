import type { components } from "../api/schema";

type Subject = components["schemas"]["SubjectRead"];

/** A row of subject pills — shared between the Lessons, Dashboard, and Uploads pages, which
 * all narrow their content to one subject (or, with `allLabel`, no subject) at a time. */
export function SubjectPicker({
  subjects,
  selectedId,
  onSelect,
  allLabel,
}: {
  subjects: Subject[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  /** Renders a leading pill that selects `null` — e.g. "All" (Uploads' unscoped filter). */
  allLabel?: string;
}) {
  // A choice of one is not a choice: name the subject so the page still says what it shows.
  if (!allLabel && subjects.length === 1) {
    return <p className="text-h3">{subjects[0].name}</p>;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {allLabel && (
        <button
          type="button"
          aria-pressed={selectedId === null}
          onClick={() => onSelect(null)}
          className={`text-body rounded-field border px-3 py-1.5 transition-colors ${
            selectedId === null
              ? "border-primary bg-primary/10 text-primary"
              : "border-base-300 hover:bg-base-200"
          }`}
        >
          {allLabel}
        </button>
      )}
      {subjects.map((s) => (
        <button
          key={s.id}
          type="button"
          aria-pressed={s.id === selectedId}
          onClick={() => onSelect(s.id)}
          className={`text-body rounded-field border px-3 py-1.5 transition-colors ${
            s.id === selectedId
              ? "border-primary bg-primary/10 text-primary"
              : "border-base-300 hover:bg-base-200"
          }`}
        >
          {s.name}
        </button>
      ))}
    </div>
  );
}
