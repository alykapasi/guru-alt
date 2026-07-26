import { masteryPercent, masteryQualifier } from "../../lib/mastery";

/** The dashboard's "mastery progress ring" (design brief: moderate gamification, tied to
 * learning, not time-on-app). */
export function MasteryRing({
  ability,
  uncertainty,
  size = 120,
}: {
  ability: number;
  uncertainty: number;
  size?: number;
}) {
  const percent = masteryPercent(ability);
  const stroke = 8;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - percent / 100);

  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={stroke}
          fill="none"
          className="stroke-base-300"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={stroke}
          fill="none"
          className="stroke-primary"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-h3">{Math.round(percent)}%</span>
        <span className="text-base-content/50 text-[11px] tracking-wide uppercase">
          {masteryQualifier(uncertainty)}
        </span>
      </div>
    </div>
  );
}
