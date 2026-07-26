import { useState } from "react";
import { useSubjects } from "../../api/hooks";
import { SubjectPicker } from "../SubjectPicker";
import { SubjectMasteryView } from "./SubjectMasteryView";

export function MasterySection() {
  const { data: subjects } = useSubjects();
  const [pickedId, setPickedId] = useState<string | null>(null);
  const selectedId = pickedId ?? subjects?.[0]?.id ?? null;

  return (
    <div className="border-base-300 flex flex-col gap-5 rounded-box border p-6">
      <h2 className="text-h2">Mastery</h2>
      {!subjects || subjects.length === 0 ? (
        <p className="text-caption text-base-content/50">No subjects yet.</p>
      ) : (
        <>
          <SubjectPicker subjects={subjects} selectedId={selectedId} onSelect={setPickedId} />
          {selectedId && <SubjectMasteryView key={selectedId} subjectId={selectedId} />}
        </>
      )}
    </div>
  );
}
