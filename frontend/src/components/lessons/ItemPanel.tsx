import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, CircleCheck, RotateCcw, Sprout } from "lucide-react";
import { useCreateConversation, useKC, useLessonPlan } from "../../api/hooks";
import { RichText } from "../content/RichText";
import { FlashcardPanel } from "./FlashcardPanel";
import type { ItemEvent } from "../../api/sse";

// The workflow's "mastered" detail means this one item was graded correct, ending the round
// early (see app/agent/workflow.py's route_after_respond) — it is NOT the same as the lesson
// plan's KC-level mastery (app/services/lesson_plan.py's ability/uncertainty thresholds, which
// need accumulated evidence across items). Copy here must stay honest about that distinction.
const DETAIL_COPY: Record<string, { label: string; icon: typeof Sprout }> = {
  mastered: { label: "Correct — nice work!", icon: Sprout },
  capped: { label: "Good effort — we'll revisit this soon", icon: RotateCcw },
};

/** What a finished round leads to: the next question in the same subject when one is due, so
 * practice continues from here rather than by way of the lesson plan. */
function NextQuestion({ subjectId }: { subjectId: string | null }) {
  const { data: plan, isLoading } = useLessonPlan(subjectId ?? undefined);
  const create = useCreateConversation();
  const navigate = useNavigate();
  const due = plan?.steps.some((s) => s.status === "active") ?? false;

  if (subjectId === null || isLoading) return null;
  if (!due) {
    return <p className="text-caption text-base-content/70">Nothing else is due right now.</p>;
  }
  return (
    <button
      type="button"
      className="btn btn-primary btn-sm"
      disabled={create.isPending}
      onClick={() =>
        create.mutate(
          { subject_id: subjectId, kind: "session", title: "Practice" },
          { onSuccess: (created) => navigate(`/app/lessons/session/${created.id}`) },
        )
      }
    >
      Next question
      <ArrowRight size={14} aria-hidden />
    </button>
  );
}

/** The persistent side panel next to a guided-practice session's transcript — the current
 * practice item plus its outcome once the workflow ends (see MASTERPLAN's gamification
 * decision: a clear, understated success state, not a celebratory animation). */
export function ItemPanel({
  item,
  detail,
  onRate,
  ratingDisabled = false,
  finished = false,
  subjectId = null,
}: {
  item: ItemEvent | null;
  detail: string | null;
  onRate: (rating: number) => void;
  /** The round is over — graded, skipped, or found finished on reopening. */
  finished?: boolean;
  /** The session's subject, where the next question comes from. */
  subjectId?: string | null;
  /** A turn is already in flight, so `onRate` would be swallowed — see Session.tsx. */
  ratingDisabled?: boolean;
}) {
  const kcId = item?.kcs[0]?.kc_id;
  const { data: kc } = useKC(kcId);
  const outcome = detail ? DETAIL_COPY[detail] : undefined;

  return (
    <div className="bg-base-100 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
      {/* Reopened after it ended there is no question left to show, only what comes next. */}
      {(item || !finished) && (
        <>
          <h3 className="text-h3 flex items-center gap-2">
            <CircleCheck size={16} className="text-primary" />
            Practice item
          </h3>
          {!item ? (
            <p className="text-caption text-base-content/70">Preparing your practice…</p>
          ) : (
            <div className="flex flex-col gap-3">
              <p className="text-caption text-base-content/70">{kc?.name ?? "…"}</p>
              {item.item_type === "flashcard" ? (
                // Keyed on item.id so a new flashcard remounts FlashcardPanel fresh — its local
                // `back`/`busy` state must not survive a prop change, or the next card renders
                // beside the previous one's revealed answer (same remount-on-id rationale as
                // App.tsx's ChatRoute/SessionRoute).
                <FlashcardPanel
                  key={item.id}
                  item={item}
                  onRate={onRate}
                  disabled={ratingDisabled}
                />
              ) : (
                <RichText content={item.stem} className="text-base-content/90" />
              )}
            </div>
          )}
        </>
      )}
      {(outcome || finished) && (
        <div className="border-base-300 mt-auto flex flex-col gap-3 border-t pt-4">
          <p className="text-body text-primary flex items-center gap-2">
            {outcome ? <outcome.icon size={16} /> : <CircleCheck size={16} />}
            {outcome ? outcome.label : "This round is finished."}
          </p>
          <NextQuestion subjectId={subjectId} />
          <Link to="/app/lessons" className="btn btn-outline btn-sm">
            Back to lesson plan
          </Link>
        </div>
      )}
    </div>
  );
}
