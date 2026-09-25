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
 * them.
 *
 * An **external** detour (`detour_reason === "external"`) is a cross-subject prerequisite: the
 * planner reached into another subject for something the learner already showed there (S24).
 * It gets its own label — the subject it came from, not "Detour" — and its own caption, since
 * the usual struggle wording ("proving hard", "clearing the way") is wrong for a step that was
 * never a struggle here at all; the raw `detour_reason` ("external") is suppressed rather than
 * printed as if it were a human-readable reason.
 *
 * `check_first` marks a component the planner is giving a head start on rather than teaching
 * from scratch, because a suggestion from another subject was accepted (S24). Any row still in
 * play shows this under its own caption, independent of whether it is also a detour. */
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
  const isExternal = isDetour && step.detour_reason === "external";
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
            : isExternal
              ? `From ${step.source_subject_name ?? "another subject"}`
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
            {isExternal
              ? blockedName
                ? `Needed for ${blockedName}.`
                : "Needed for a later step."
              : isProposed
                ? `Offered because ${blockedName ?? "…"} is proving hard.`
                : isDisproved
                  ? blockedName
                    ? `Turned out not to be the gap — back to ${blockedName}.`
                    : "Turned out not to be the gap."
                  : blockedName
                    ? `Clearing the way back to ${blockedName}.`
                    : "Clearing the way."}
            {!isExternal && !isProposed && !isDisproved && step.detour_reason
              ? ` ${step.detour_reason}`
              : ""}
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
      {!isSettled && step.check_first && (
        <p className="text-caption text-primary pl-9">Confirming what you already know</p>
      )}
    </div>
  );
}
