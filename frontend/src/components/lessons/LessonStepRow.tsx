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
 * this (`detour_for`, `detour_reason`) are what the row says out loud.
 *
 * In exploration guidance (V07) a detour is only *proposed* until the learner decides — the row
 * offers "Take detour" / "Skip" for that, and "Skip" alone once it is under way (`active`/
 * `pending`). `onDecide`/`deciding` are only meaningful for a detour row; other rows ignore
 * them. */
export function LessonStepRow({
  step,
  onDecide,
  deciding,
}: {
  step: LessonStep;
  onDecide?: (decision: "accept" | "skip") => void;
  deciding?: boolean;
}) {
  const { data: kc } = useKC(step.kc_id);
  // The component the learner was actually working towards when this detour was inserted.
  const { data: blockedKc } = useKC(step.detour_for ?? undefined);
  const isActive = step.status === "active";
  const isDone = step.status === "done";
  const isSkipped = step.status === "skipped";
  const isProposed = step.status === "proposed";
  const isDetour = step.step_type === "detour";
  // Struck through once the row is settled — completed or dismissed — same as "done" always was.
  const isSettled = isDone || isSkipped;
  const isDisproved = isDone && step.detour_outcome === "disproved";
  // Open to a decision until it resolves (done) or is dismissed (skipped); proposed additionally
  // offers "take", active/pending offer only "skip".
  const decidable = isDetour && !isDone && !isSkipped;
  const blockedName = blockedKc?.name;

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
          } ${isSkipped ? "opacity-50" : ""}`}
        >
          {isDone ? <Check size={12} /> : isDetour ? <CornerDownRight size={12} /> : null}
        </span>
        <span
          className={`text-body flex-1 truncate ${
            isSettled ? "text-base-content/40 line-through" : "text-base-content/90"
          }`}
        >
          {kc?.name ?? "…"}
        </span>
        <span
          className={`text-caption shrink-0 ${isDetour ? "text-warning" : "text-base-content/50"}`}
        >
          {isSkipped
            ? "Skipped"
            : isDetour
              ? "Detour"
              : step.step_type === "review"
                ? "Review"
                : "New"}
        </span>
      </div>
      {isDetour && (
        <div className="flex items-center justify-between gap-2 pl-9">
          <p className="text-caption text-base-content/60">
            {isProposed
              ? `Offered because ${blockedName ?? "…"} is proving hard.`
              : isDisproved
                ? blockedName
                  ? `Turned out not to be the gap — back to ${blockedName}.`
                  : "Turned out not to be the gap."
                : blockedName
                  ? `Clearing the way back to ${blockedName}.`
                  : "Clearing the way."}
            {!isProposed && !isDisproved && step.detour_reason ? ` ${step.detour_reason}` : ""}
          </p>
          {decidable && (
            <div className="flex shrink-0 gap-1">
              {isProposed && (
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  disabled={deciding}
                  onClick={() => onDecide?.("accept")}
                >
                  Take detour
                </button>
              )}
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                disabled={deciding}
                onClick={() => onDecide?.("skip")}
              >
                Skip
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
