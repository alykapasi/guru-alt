import { CalendarClock } from "lucide-react";
import { useKC, useReviewsDue } from "../../api/hooks";
import type { components } from "../../api/schema";

type ReviewItem = components["schemas"]["ReviewItemRead"];

function ReviewRow({ review }: { review: ReviewItem }) {
  const { data: kc } = useKC(review.kc_id);
  const dueDate = new Date(review.due_at);
  const overdueDays = Math.max(
    0,
    Math.floor((Date.now() - dueDate.getTime()) / (1000 * 60 * 60 * 24)),
  );

  return (
    <div className="flex items-center justify-between rounded-field px-3 py-2">
      <span className="text-body truncate">{kc?.name ?? "…"}</span>
      <span className="text-caption text-base-content/50 shrink-0">
        {overdueDays === 0 ? "due today" : `due ${overdueDays}d ago`}
      </span>
    </div>
  );
}

/** The FSRS due-for-review queue, global across every subject — retention is the point of the
 * spaced-repetition scheduler, so seeing what's coming due belongs on the dashboard even though
 * mastery below is broken out per subject. */
export function ReviewsDueCard() {
  const { data } = useReviewsDue();

  return (
    <div className="border-base-300 flex flex-col gap-3 rounded-box border p-6">
      <h2 className="text-h2 flex items-center gap-2">
        <CalendarClock size={18} className="text-primary" />
        Due for review
      </h2>
      {!data || data.length === 0 ? (
        <p className="text-caption text-base-content/50">Nothing due right now.</p>
      ) : (
        <div className="flex flex-col gap-1">
          {data.map((review) => (
            <ReviewRow key={review.kc_id} review={review} />
          ))}
        </div>
      )}
    </div>
  );
}
