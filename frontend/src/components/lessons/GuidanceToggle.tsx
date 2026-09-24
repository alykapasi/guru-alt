type Guidance = "guided" | "exploration";

const OPTIONS: { value: Guidance; label: string }[] = [
  { value: "guided", label: "Guided" },
  { value: "exploration", label: "Exploration" },
];

const CAPTION: Record<Guidance, string> = {
  guided: "Guru can add a detour on its own when you are stuck.",
  exploration: "Guru offers detours; you decide.",
};

/** How much the planner may decide for the learner on its own (V07/S11): guided takes a detour
 * without asking, exploration only offers one and waits on the learner. A segmented control
 * rather than a checkbox because these are two named modes, not an on/off switch. */
export function GuidanceToggle({
  value,
  onChange,
  disabled,
}: {
  value: Guidance;
  onChange: (value: Guidance) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div role="radiogroup" aria-label="Guidance" className="join w-fit">
        {OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={value === option.value}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className={`btn btn-xs join-item ${
              value === option.value ? "btn-active" : "btn-ghost"
            }`}
          >
            {option.label}
          </button>
        ))}
      </div>
      <p className="text-caption text-base-content/60">{CAPTION[value]}</p>
    </div>
  );
}
