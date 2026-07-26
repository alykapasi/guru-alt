import { Flame, TrendingDown, TrendingUp, Minus } from "lucide-react";
import { useActivity } from "../../api/hooks";

const MOMENTUM_COPY: Record<string, { label: string; icon: typeof TrendingUp }> = {
  up: { label: "Picking up pace", icon: TrendingUp },
  down: { label: "Slowing down lately", icon: TrendingDown },
  steady: { label: "Steady pace", icon: Minus },
  none: { label: "No recent practice", icon: Minus },
};

/** A practice streak (consecutive days with real graded activity, not app opens) plus a
 * momentum trend — the "moderate, mastery-tied" gamification the design brief called for. */
export function ActivityCard() {
  const { data } = useActivity();
  if (!data) return null;
  const momentum = MOMENTUM_COPY[data.momentum] ?? MOMENTUM_COPY.none;

  return (
    <div className="border-base-300 flex items-center gap-8 rounded-box border p-6">
      <div className="flex items-center gap-3">
        <span className="bg-accent/15 text-accent flex size-11 items-center justify-center rounded-full">
          <Flame size={20} />
        </span>
        <div>
          <p className="text-h2 leading-none">{data.streak_days}</p>
          <p className="text-caption text-base-content/60">day streak</p>
        </div>
      </div>
      <div className="border-base-300 flex items-center gap-2 border-l pl-8">
        <momentum.icon size={16} className="text-base-content/50" />
        <div>
          <p className="text-body">{momentum.label}</p>
          <p className="text-caption text-base-content/50">
            {data.observations_last_7d} practiced this week vs {data.observations_prior_7d} last
            week
          </p>
        </div>
      </div>
    </div>
  );
}
