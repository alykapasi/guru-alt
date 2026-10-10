import { usePreferences, useSetPreference } from "../../api/hooks";
import { VALUE_CAPTIONS, optionLabel } from "./preferenceCopy";

/** One control per setting (S02, V09). Without `subjectId` it edits the learner's defaults;
 * with one, that subject's overrides — each showing the default it would otherwise use. */
export function PreferenceControls({ subjectId }: { subjectId?: string }) {
  const { data } = usePreferences(subjectId);
  const set = useSetPreference(subjectId);
  if (!data) return null;
  return (
    <div className="flex flex-col gap-3">
      {data.map((pref) => {
        const overridden = subjectId !== undefined && pref.source === "subject";
        return (
          <div key={pref.key} className="flex flex-col gap-1">
            <label className="text-body flex items-center justify-between gap-3">
              <span>{pref.label}</span>
              <select
                aria-label={pref.label}
                className="select select-sm"
                value={pref.value}
                disabled={set.isPending}
                onChange={(e) => set.mutate({ key: pref.key, value: e.target.value })}
              >
                {pref.options.map((option) => (
                  <option key={option} value={option}>
                    {optionLabel(option)}
                  </option>
                ))}
              </select>
            </label>
            {VALUE_CAPTIONS[pref.value] && (
              <p className="text-caption text-base-content/70">{VALUE_CAPTIONS[pref.value]}</p>
            )}
            <p className="text-caption text-base-content/70">
              {subjectId !== undefined && !overridden && pref.global_value !== null
                ? `Using your default (${optionLabel(pref.global_value)}).`
                : pref.value === "auto" && pref.inferred
                  ? `Adapting to you: ${optionLabel(pref.inferred).toLowerCase()} for now.`
                  : null}
              {overridden && (
                <button
                  type="button"
                  className="link ml-1"
                  aria-label={`Use my default for ${pref.label}`}
                  onClick={() => set.mutate({ key: pref.key, value: null })}
                >
                  Use my default
                </button>
              )}
            </p>
          </div>
        );
      })}
    </div>
  );
}
