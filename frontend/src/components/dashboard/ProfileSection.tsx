import { RotateCcw, Sparkles } from "lucide-react";
import { useProfile, useRefreshProfile, useResetDimension } from "../../api/hooks";
import { formatDimensionValue, humanizeKey } from "../../lib/profile";

/** The learner profile — "how you learn," evidence-based behavioral dimensions, not VARK (see
 * MASTERPLAN §4/CLAUDE.md's key decisions). Generic key/value rendering since each dimension's
 * value is independently shaped (see app/learning/profile_estimators.py).
 *
 * Each row shows the backend's `label` and `observation` rather than a title-cased key (S44).
 * These are proxies, not measured traits: "Reading level: 8.5" reads as a finding about the
 * learner, when the number is a readability score of their own chat messages. The catalog
 * carries the honest description; this only has to show it. */
export function ProfileSection() {
  const { data } = useProfile();
  const refresh = useRefreshProfile();
  const reset = useResetDimension();

  return (
    <div className="border-base-300 flex flex-col gap-4 rounded-box border p-6">
      <div className="flex items-center justify-between">
        <h2 className="text-h2">Your learner profile</h2>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          className="btn btn-outline btn-sm"
        >
          <Sparkles size={14} />
          Refresh
        </button>
      </div>
      {!data || data.dimensions.length === 0 ? (
        <p className="text-caption text-base-content/50">
          Nothing learned about how you learn yet — practice a bit, then refresh.
        </p>
      ) : (
        <div className="flex flex-col gap-1">
          {data.dimensions.map((d) => (
            <div
              key={d.key}
              className="hover:bg-base-200 flex items-start justify-between gap-4 rounded-field px-3 py-2"
            >
              <div className="min-w-0">
                <p className="text-body truncate">{d.label || humanizeKey(d.key)}</p>
                <p className="text-caption text-base-content/60 truncate">
                  {formatDimensionValue(d.value)}
                </p>
                {d.observation && (
                  <p className="text-caption text-base-content/40 mt-0.5">{d.observation}</p>
                )}
              </div>
              <button
                onClick={() => reset.mutate(d.key)}
                aria-label={`Reset ${d.label || humanizeKey(d.key)}`}
                className="hover:bg-base-300 shrink-0 rounded-field p-1.5"
              >
                <RotateCcw size={13} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
