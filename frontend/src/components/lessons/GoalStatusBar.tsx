import type { components } from "../../api/schema";

type GoalStatus = components["schemas"]["GoalStatusRead"];

/** What the learner has demonstrated toward their goal, and how current it still is (S01/S63).
 *
 * Four facts, not one state. A goal can be achieved and stale, or closed and unfinished, so
 * the headline and the staleness note are rendered independently rather than collapsed into
 * a single label.
 *
 * The copy must not promise knowledge. "Demonstrated across independent checks" is the claim
 * the evidence supports; "you know this" is not — the estimate cannot support it, and S46's
 * honest-display rule is what this panel inherits.
 */
export function GoalStatusBar({
  status,
  deferredCount,
}: {
  status: GoalStatus;
  deferredCount: number;
}) {
  // 0 means this plan predates recorded objectives — "we do not know", not "nothing left".
  // A full-looking empty bar is the most misleading thing this panel could show.
  if (status.objective_kc_count === 0) return null;

  const achieved = status.achieved_kc_count;
  const total = status.objective_kc_count;
  const pct = Math.round((achieved / total) * 100);

  return (
    <div className="border-base-300 flex flex-col gap-2 rounded-box border p-4">
      {status.closed_at ? (
        <p className="text-body">You closed this goal.</p>
      ) : status.achieved_at ? (
        <p className="text-body text-primary">
          Goal complete — demonstrated across independent checks.
        </p>
      ) : (
        <p className="text-body">
          {achieved} of {total} components demonstrated across independent checks.
        </p>
      )}

      <div
        className="bg-base-200 h-2 w-full overflow-hidden rounded-full"
        role="progressbar"
        aria-valuenow={achieved}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label="Components demonstrated"
      >
        <div className="bg-primary h-full" style={{ width: `${pct}%` }} />
      </div>

      {/* Not a segment of the bar: achieved and stale overlap, so stacking them would imply
          they partition. Reported only — nothing reopens a step because evidence aged. */}
      {status.stale_kc_count > 0 && (
        <p className="text-caption text-warning">
          {status.stale_kc_count} component{status.stale_kc_count === 1 ? "" : "s"} last checked a
          while ago — worth checking again.
        </p>
      )}

      {deferredCount > 0 && (
        <p className="text-caption text-base-content/60">
          {deferredCount} more {deferredCount === 1 ? "component is" : "components are"} part of
          this goal but not in the current window.
        </p>
      )}
    </div>
  );
}
