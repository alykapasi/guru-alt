import { Check } from "lucide-react";
import { useKC } from "../../api/hooks";
import type { components } from "../../api/schema";

type LessonStep = components["schemas"]["LessonStepRead"];

export function LessonStepRow({ step }: { step: LessonStep }) {
  const { data: kc } = useKC(step.kc_id);
  const isActive = step.status === "active";
  const isDone = step.status === "done";

  return (
    <div
      className={`flex items-center gap-3 rounded-field px-3 py-2 ${isActive ? "bg-primary/10" : ""}`}
    >
      <span
        className={`flex size-6 shrink-0 items-center justify-center rounded-full ${
          isDone ? "bg-primary/15 text-primary" : "border-base-300 border"
        }`}
      >
        {isDone && <Check size={12} />}
      </span>
      <span
        className={`text-body flex-1 truncate ${
          isDone ? "text-base-content/40 line-through" : "text-base-content/90"
        }`}
      >
        {kc?.name ?? "…"}
      </span>
      <span className="text-caption text-base-content/50 shrink-0">
        {step.step_type === "review" ? "Review" : "New"}
      </span>
    </div>
  );
}
