import { Check, CornerDownRight } from "lucide-react";
import { useKC } from "../../api/hooks";
import type { components } from "../../api/schema";

type LessonStep = components["schemas"]["LessonStepRead"];

/** One row of the lesson plan.
 *
 * A **detour** is rendered differently from every other step on purpose (S11). The planner
 * can reorder the plan to put a prerequisite first when a learner is stuck, and a reordering
 * nobody explains is indistinguishable from the plan changing its mind — which is the thing a
 * learner would reasonably lose trust over. The two fields the planner records for exactly
 * this (`detour_for`, `detour_reason`) are what the row says out loud. */
export function LessonStepRow({ step }: { step: LessonStep }) {
  const { data: kc } = useKC(step.kc_id);
  // The component the learner was actually working towards when this detour was inserted.
  const { data: blockedKc } = useKC(step.detour_for ?? undefined);
  const isActive = step.status === "active";
  const isDone = step.status === "done";
  const isDetour = step.step_type === "detour";

  return (
    <div
      className={`flex flex-col gap-1 rounded-field px-3 py-2 ${isActive ? "bg-primary/10" : ""}`}
    >
      <div className="flex items-center gap-3">
        <span
          className={`flex size-6 shrink-0 items-center justify-center rounded-full ${
            isDone
              ? "bg-primary/15 text-primary"
              : isDetour
                ? "bg-warning/15 text-warning"
                : "border-base-300 border"
          }`}
        >
          {isDone ? <Check size={12} /> : isDetour ? <CornerDownRight size={12} /> : null}
        </span>
        <span
          className={`text-body flex-1 truncate ${
            isDone ? "text-base-content/40 line-through" : "text-base-content/90"
          }`}
        >
          {kc?.name ?? "…"}
        </span>
        <span
          className={`text-caption shrink-0 ${isDetour ? "text-warning" : "text-base-content/50"}`}
        >
          {isDetour ? "Detour" : step.step_type === "review" ? "Review" : "New"}
        </span>
      </div>
      {isDetour && (
        <p className="text-caption text-base-content/60 pl-9">
          {blockedKc?.name ? `Clearing the way back to ${blockedKc.name}.` : "Clearing the way."}
          {step.detour_reason ? ` ${step.detour_reason}` : ""}
        </p>
      )}
    </div>
  );
}
