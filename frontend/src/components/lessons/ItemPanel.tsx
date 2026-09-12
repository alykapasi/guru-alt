import { Link } from "react-router-dom";
import { CircleCheck, RotateCcw, Sprout } from "lucide-react";
import { useKC } from "../../api/hooks";
import { RichText } from "../content/RichText";
import type { ItemEvent } from "../../api/sse";

// The workflow's "mastered" detail means this one item was graded correct, ending the round
// early (see app/agent/workflow.py's route_after_respond) — it is NOT the same as the lesson
// plan's KC-level mastery (app/services/lesson_plan.py's ability/uncertainty thresholds, which
// need accumulated evidence across items). Copy here must stay honest about that distinction.
const DETAIL_COPY: Record<string, { label: string; icon: typeof Sprout }> = {
  mastered: { label: "Correct — nice work!", icon: Sprout },
  capped: { label: "Good effort — we'll revisit this soon", icon: RotateCcw },
};

/** The persistent side panel next to a guided-practice session's transcript — the current
 * practice item plus its outcome once the workflow ends (see MASTERPLAN's gamification
 * decision: a clear, understated success state, not a celebratory animation). */
export function ItemPanel({ item, detail }: { item: ItemEvent | null; detail: string | null }) {
  const kcId = item?.kcs[0]?.kc_id;
  const { data: kc } = useKC(kcId);
  const outcome = detail ? DETAIL_COPY[detail] : undefined;

  return (
    <div className="bg-base-100 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
      <h3 className="text-h3 flex items-center gap-2">
        <CircleCheck size={16} className="text-primary" />
        Practice item
      </h3>
      {!item ? (
        <p className="text-caption text-base-content/50">Preparing your practice…</p>
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-caption text-base-content/60">{kc?.name ?? "…"}</p>
          <RichText content={item.stem} className="text-base-content/90" />
        </div>
      )}
      {outcome && (
        <div className="border-base-300 mt-auto flex flex-col gap-3 border-t pt-4">
          <p className="text-body text-primary flex items-center gap-2">
            <outcome.icon size={16} />
            {outcome.label}
          </p>
          <Link to="/app/lessons" className="btn btn-outline btn-sm">
            Back to lesson plan
          </Link>
        </div>
      )}
    </div>
  );
}
